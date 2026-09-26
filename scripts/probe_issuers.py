"""Probe issuer product pages: is the bonus / annual fee / earn rate readable
from a single plain HTTP GET (static HTML, JSON-LD, __NEXT_DATA__, inline state)?

Polite by construction: a self-identifying User-Agent, robots.txt respected,
one GET per page, at least 2 seconds between requests to the same host, no
retries, no cookies, no JavaScript. Also checks the aggregator RSS feeds.

Usage:
    python scripts/probe_issuers.py                    # markdown table to stdout
    python scripts/probe_issuers.py --json out.json    # full results
    python scripts/probe_issuers.py --write-findings   # update docs/FINDINGS.md

Pages come from config/card_sources.yaml. Besides the regex facts it reports
what the card-terms pipeline will see: the extracted text length, its hash, and
whether the card's name appears on the page at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import feedparser
import httpx
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from card_agent.collector.doc_rss import FEED_URL  # noqa: E402
from card_agent.collector.http import HostThrottle, RobotsCache, make_client  # noqa: E402
from card_agent.collector.issuer_pages import analyze_page, fetch_html  # noqa: E402
from card_agent.terms.page import MIN_TEXT_CHARS, content_hash, page_text  # noqa: E402
from card_agent.terms.sources import load_sources, normalize_card_name  # noqa: E402

FEEDS = [FEED_URL, "https://www.doctorofcredit.com/feed/"]

START_MARKER = "<!-- probe:start -->"
END_MARKER = "<!-- probe:end -->"


def probe_pages(client: httpx.Client, pages: list[dict]) -> list[dict]:
    """One GET per page; report both the regex facts and the extraction text."""
    robots = RobotsCache(client)
    throttle = HostThrottle(min_interval=2.0)
    results = []
    for page in pages:
        status, body, error = fetch_html(client, robots, throttle, page["url"])
        if error:
            analysis = analyze_page(None, "")
            analysis.blocked, analysis.note = True, error
            text = ""
        else:
            analysis = analyze_page(status, body)
            text = page_text(body) if status and status < 400 else ""
        normalized_text = f" {normalize_card_name(text)} "
        name_found = any(
            f" {normalize_card_name(name)} " in normalized_text for name in page["page_names"]
        )
        row = {
            **page,
            **analysis.to_dict(),
            "text_chars": len(text),
            "sha256": content_hash(text)[:12] if text else None,
            "name_found": name_found,
            "extraction_ready": bool(text) and len(text) >= MIN_TEXT_CHARS and not analysis.blocked,
        }
        results.append(row)
        print(
            f"[{page['issuer']}] {page['card_id']}: status={analysis.status_code} "
            f"text_chars={len(text)} name_found={name_found} blocked={analysis.blocked} "
            f"js_only={analysis.js_only} fields={analysis.fields_available} note={analysis.note}",
            file=sys.stderr,
        )
        print(f"    text: {text[:240]!r}", file=sys.stderr)
    return results


def probe_feeds(client: httpx.Client, feeds: list[str]) -> list[dict]:
    robots = RobotsCache(client)
    results = []
    for url in feeds:
        allowed, reason = robots.allowed(url)
        if not allowed:
            results.append({"url": url, "ok": False, "note": reason})
            continue
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            results.append(
                {"url": url, "ok": False, "note": f"request failed: {type(exc).__name__}"}
            )
            continue
        parsed = feedparser.parse(response.content)
        entries = parsed.entries
        newest = entries[0].get("published") if entries else None
        oldest = entries[-1].get("published") if entries else None
        results.append(
            {
                "url": url,
                "ok": response.status_code == 200 and bool(entries),
                "status_code": response.status_code,
                "entries": len(entries),
                "newest": newest,
                "oldest": oldest,
                "sample_titles": [e.get("title") for e in entries[:8]],
                "sample_tags": [[t.get("term") for t in e.get("tags", [])] for e in entries[:3]],
                "note": None if entries else "no entries parsed",
            }
        )
        print(f"[feed] {url}: {results[-1]}", file=sys.stderr)
    return results


def verdict(row: dict) -> str:
    if row["status_code"] and row["status_code"] >= 400 and not row["blocked"]:
        return f"not found (HTTP {row['status_code']}; URL moved)"
    if row["blocked"]:
        return f"blocked ({row['note']})"
    if row["js_only"] or not row["extraction_ready"]:
        return "JS-only / too little text"
    if not row["name_found"]:
        return "readable, but the card's name isn't on the page"
    return "usable"


def issuer_table(results: list[dict]) -> str:
    frame = pd.DataFrame(results)
    frame["fields"] = frame["fields_available"].map(lambda f: ", ".join(f) if f else "none")
    frame["verdict"] = frame.apply(verdict, axis=1)
    frame = frame.sort_values(["issuer", "card_id"])
    lines = [
        "| issuer | card | HTTP | text chars | card name on page | regex fields | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in frame.itertuples():
        found = "yes" if row.name_found else "no"
        lines.append(
            f"| {row.issuer} | {row.card_id} | {row.status_code or '–'} | {row.text_chars:,} "
            f"| {found} | {row.fields} | {row.verdict} |"
        )
    return "\n".join(lines)


def feed_table(results: list[dict]) -> str:
    lines = ["| feed | works? | entries | newest | oldest |", "|---|---|---|---|---|"]
    for row in results:
        lines.append(
            f"| {row['url']} | {'yes' if row['ok'] else 'no: ' + str(row.get('note'))} | "
            f"{row.get('entries', 0)} | {row.get('newest') or ''} | {row.get('oldest') or ''} |"
        )
    return "\n".join(lines)


def render(issuers: list[dict], feeds: list[dict]) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"_Probe run: {stamp}. One GET per page, robots.txt respected, "
        "self-identifying User-Agent, no JavaScript._\n\n"
        f"{issuer_table(issuers)}\n\n**Aggregator feeds**\n\n{feed_table(feeds)}\n"
    )


def write_findings(markdown: str, path: Path) -> None:
    text = path.read_text() if path.exists() else f"{START_MARKER}\n{END_MARKER}\n"
    before, _, rest = text.partition(START_MARKER)
    _, _, after = rest.partition(END_MARKER)
    path.write_text(f"{before}{START_MARKER}\n{markdown}\n{END_MARKER}{after}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=REPO_ROOT / "config" / "card_sources.yaml")
    parser.add_argument("--json", type=Path, help="write full results as JSON here")
    parser.add_argument("--write-findings", action="store_true", help="update docs/FINDINGS.md")
    parser.add_argument(
        "--summary", type=Path, help="append the markdown to this file (CI summary)"
    )
    args = parser.parse_args(argv)

    pages = [
        {
            "issuer": source.issuer,
            "card_id": source.card_id,
            "url": source.url,
            "page_names": source.page_names or [source.name],
        }
        for source in load_sources(args.sources).values()
        if source.url
    ]
    with make_client() as client:
        issuers = probe_pages(client, pages)
        feeds = probe_feeds(client, FEEDS)

    markdown = render(issuers, feeds)
    print(markdown)
    if args.json:
        args.json.write_text(
            json.dumps({"issuers": issuers, "feeds": feeds}, indent=2, default=str)
        )
    if args.summary:
        with args.summary.open("a") as handle:
            handle.write(markdown)
    if args.write_findings:
        write_findings(markdown, REPO_ROOT / "docs" / "FINDINGS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
