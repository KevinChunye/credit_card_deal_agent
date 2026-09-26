"""Collector run: fetch sources, normalize, diff, write snapshot files.

Output layout (committed to the `data` branch by the workflow):
    data/latest.json
    data/snapshots/YYYY-MM-DD.json
    data/changes/YYYY-MM-DD.json
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from card_agent.collector import bonuses_api, doc_rss, rewards_db
from card_agent.collector.diff import diff_snapshots, primary_offers
from card_agent.collector.http import HostThrottle, RobotsCache
from card_agent.collector.issuer_pages import cross_check, fetch_and_analyze
from card_agent.collector.seed import apply_seed, load_seed
from card_agent.config import CONFIG_DIR
from card_agent.matching import CardMatcher
from card_agent.models import ChangeSet, Snapshot, SourceStatus
from card_agent.terms.sources import load_sources


def load_previous(out_dir: Path) -> Snapshot | None:
    path = out_dir / "data" / "latest.json"
    if not path.exists():
        return None
    return Snapshot.model_validate_json(path.read_text())


def run_collector(
    out_dir: Path,
    client: httpx.Client,
    now: datetime,
    rewards_dir: Path | None = None,
    source_file: Path | None = None,
    with_rss: bool = True,
    with_issuer_pages: bool = True,
    config_dir: Path = CONFIG_DIR,
) -> tuple[Snapshot, ChangeSet]:
    previous = load_previous(out_dir)
    sources: dict[str, SourceStatus] = {}

    # 1. Bonuses API: required. If it fails the run fails, and the data branch
    #    keeps the last good snapshot.
    if source_file:
        raw_cards = json.loads(source_file.read_text())
        api_url = str(source_file)
    else:
        raw_cards = bonuses_api.fetch(client)
        api_url = bonuses_api.DATA_URL
    cards, offers, benefits, base_rates = bonuses_api.normalize(raw_cards, now)
    sources["bonuses_api"] = SourceStatus(
        status="ok", url=api_url, fetched_at=now, count=len(raw_cards)
    )

    # 2. Curated seed (earn rates, FX fees, protections, downgrade paths).
    seed = load_seed(config_dir / "card_details.yaml")
    cards, earn_rates, protections, downgrade_paths, warnings = apply_seed(seed, cards, base_rates)
    sources["card_details_seed"] = SourceStatus(
        status="ok",
        url="config/card_details.yaml",
        fetched_at=now,
        count=len(seed.get("cards") or {}),
        detail="; ".join(warnings) or f"as_of {seed.get('as_of')}",
    )

    # 3. Rewards DB: opt-in only (no license); see docs/FINDINGS.md.
    if rewards_dir:
        cards, earn_rates, benefits, protections, matched = rewards_db.merge(
            rewards_dir, cards, earn_rates, benefits, protections
        )
        sources["rewards_db"] = SourceStatus(
            status="ok", url=rewards_db.REPO_URL, fetched_at=now, count=matched
        )
    else:
        sources["rewards_db"] = SourceStatus(
            status="disabled", detail="no license; opt in with --rewards-dir"
        )

    matcher = CardMatcher(cards)

    # 4. Doctor of Credit RSS: optional. On failure keep the previous news.
    news = previous.news if previous else []
    if with_rss:
        try:
            news, pages = doc_rss.fetch_news(client, matcher, now, previous=news)
            sources["doc_rss"] = SourceStatus(
                status="ok",
                url=doc_rss.FEED_URL,
                fetched_at=now,
                count=len(news),
                detail=f"{pages} page(s) fetched",
            )
        except (httpx.HTTPError, ValueError) as exc:
            sources["doc_rss"] = SourceStatus(
                status="error", url=doc_rss.FEED_URL, detail=f"{type(exc).__name__}: {exc}"
            )
    else:
        sources["doc_rss"] = SourceStatus(status="skipped")

    # 5. Issuer pages that the probe found readable: cross-check only.
    cross_checks: list[dict[str, Any]] = []
    if with_issuer_pages:
        pages = [
            source
            for source in load_sources(config_dir / "card_sources.yaml").values()
            if source.cross_check and source.url
        ]
        best = primary_offers(Snapshot(generated_at=now, offers=offers))
        fees = {card.id: card.annual_fee for card in cards}
        robots, throttle = RobotsCache(client), HostThrottle()
        for page in pages:
            analysis = fetch_and_analyze(client, robots, throttle, page.url)
            card_id = page.card_id
            has_offer = card_id in best.index
            api_bonus = float(best.loc[card_id, "bonus_amount"]) if has_offer else None
            api_unit = best.loc[card_id, "bonus_unit"] if has_offer else None
            checks = cross_check(card_id, analysis, fees.get(card_id), api_bonus, api_unit)
            if not checks:
                checks = [{"card_id": card_id, "field": None, "match": None, "note": analysis.note}]
            cross_checks.extend({**check, "url": page.url} for check in checks)
        sources["issuer_pages"] = SourceStatus(
            status="ok", fetched_at=now, count=len(pages), detail="cross-check only"
        )
    else:
        sources["issuer_pages"] = SourceStatus(status="skipped")

    snapshot = Snapshot(
        generated_at=now,
        sources=sources,
        cards=cards,
        earn_rates=earn_rates,
        offers=offers,
        benefits=benefits,
        protections=protections,
        news=news,
        cross_checks=cross_checks,
        downgrade_paths=downgrade_paths,
    )
    changes = diff_snapshots(previous, snapshot, now.date())
    return snapshot, changes


def write_outputs(out_dir: Path, snapshot: Snapshot, changes: ChangeSet) -> dict[str, Path]:
    stamp = snapshot.generated_at.date().isoformat()
    data_dir = out_dir / "data"
    paths = {
        "latest": data_dir / "latest.json",
        "snapshot": data_dir / "snapshots" / f"{stamp}.json",
        "changes": data_dir / "changes" / f"{stamp}.json",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot.model_copy(update={"changes_file": f"data/changes/{stamp}.json"})
    body = snapshot.model_dump_json(indent=1)
    paths["latest"].write_text(body)
    paths["snapshot"].write_text(body)
    paths["changes"].write_text(changes.model_dump_json(indent=1))
    return paths


def summary_markdown(snapshot: Snapshot, changes: ChangeSet) -> str:
    lines = [
        f"## Collector run {snapshot.generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "| source | status | count | detail |",
        "|---|---|---|---|",
    ]
    for name, status in snapshot.sources.items():
        lines.append(f"| {name} | {status.status} | {status.count} | {status.detail or ''} |")
    lines += [
        "",
        f"Cards: {len(snapshot.cards)} · offers: {len(snapshot.offers)} · "
        f"earn rates: {len(snapshot.earn_rates)} · benefits: {len(snapshot.benefits)} · "
        f"news (35d): {len(snapshot.news)}",
        "",
        f"Changes vs {changes.previous_date or 'nothing (first run = baseline)'}: "
        f"{len(changes.new_cards)} new cards, {len(changes.elevated_bonuses)} elevated, "
        f"{len(changes.reduced_bonuses)} reduced, {len(changes.fee_changes)} fee changes, "
        f"{len(changes.new_offers)} new offers, {len(changes.removed_offers)} removed offers",
    ]
    mismatches = [c for c in snapshot.cross_checks if c.get("match") is False]
    if mismatches:
        lines += ["", "**Issuer page cross-check mismatches**", ""]
        lines += [
            f"- {c['card_id']} {c['field']}: API {c['api_value']} vs page {c['page_value']} "
            f"({c.get('evidence', '')[:100]})"
            for c in mismatches
        ]
    return "\n".join(lines) + "\n"
