"""Fetch an issuer page and reduce it to the text we hash and extract from.

Text = visible text (scripts, styles, nav and footnote superscripts removed)
plus human-readable strings from embedded JSON (JSON-LD, __NEXT_DATA__),
whitespace-normalized. The same text is what the LLM sees and what evidence
quotes are checked against, so a quote can only pass if it's really there.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass

import httpx

from card_agent.collector.http import HostThrottle, RobotsCache
from card_agent.collector.issuer_pages import (
    BLOCK_MARKERS,
    fetch_html,
    flatten_strings,
    inline_state_chunks,
    json_ld_blocks,
    next_data,
)

MAX_TEXT_CHARS = 80_000
MIN_TEXT_CHARS = 1_500
EMBEDDED_HEADER = "[Embedded page data]"
STATE_HEADER = "[Embedded page state]"


def visible_text(page: str) -> str:
    page = re.sub(
        r"<(script|style|noscript|template|svg|nav)\b.*?</\1>", " ", page, flags=re.S | re.I
    )
    page = re.sub(r"<sup\b.*?</sup>", " ", page, flags=re.S | re.I)  # footnote markers
    page = re.sub(r"<[^>]+>", " ", page)
    return normalize_whitespace(html_lib.unescape(page))


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _human_strings(obj) -> list[str]:
    """Strings from embedded JSON that read like prose, not ids, URLs or code."""
    keep = []
    seen = set()
    for value in flatten_strings(obj):
        text = normalize_whitespace(html_lib.unescape(re.sub(r"<[^>]+>", " ", value)))
        if (
            len(text.split()) >= 3
            and len(text) <= 2000
            and re.search(r"[A-Za-z]", text)
            and not text.startswith(("http://", "https://", "/", "{", "["))
            and text not in seen
        ):
            seen.add(text)
            keep.append(text)
    return keep


def page_text(page: str) -> str:
    embedded = _human_strings(json_ld_blocks(page)) + _human_strings(next_data(page) or {})
    text = visible_text(page)
    if embedded:
        text = f"{text} {EMBEDDED_HEADER} {' | '.join(embedded)}"
    if len(text) < MIN_TEXT_CHARS:
        # Client-rendered pages (e.g. some Amex pages) ship their copy only in a
        # `window.__STATE__ = ...` blob. Use its prose only when the page has
        # almost no visible text, so normal pages stay lean and hash-stable.
        quoted = [
            match
            for chunk in inline_state_chunks(page)
            for match in re.findall(r'"((?:[^"\\]|\\.){15,2000})"', chunk)
        ]
        state = _human_strings(quoted)
        if state:
            text = f"{text} {STATE_HEADER} {' | '.join(state)}"
    return normalize_whitespace(text)[:MAX_TEXT_CHARS]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class FetchedPage:
    url: str
    status_code: int | None = None
    text: str = ""
    sha256: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_page(
    client: httpx.Client, robots: RobotsCache, throttle: HostThrottle, url: str
) -> FetchedPage:
    status, body, error = fetch_html(client, robots, throttle, url)
    if error:
        return FetchedPage(url=url, error=error)
    if status is None or status >= 400:
        return FetchedPage(url=url, status_code=status, error=f"HTTP {status}")
    text = page_text(body)
    if len(text) < MIN_TEXT_CHARS:
        lowered = body[:200_000].lower()
        marker = next((m for m in BLOCK_MARKERS if m in lowered), None)
        reason = (
            f"bot-protection page ({marker!r})"
            if marker
            else "almost no text (JavaScript-rendered?)"
        )
        return FetchedPage(url=url, status_code=status, text=text, error=reason)
    return FetchedPage(url=url, status_code=status, text=text, sha256=content_hash(text))
