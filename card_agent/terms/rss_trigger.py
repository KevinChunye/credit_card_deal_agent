"""RSS trigger: queue a card for forced re-extraction when Doctor of Credit
posts about a change to it (refresh, new benefits, devaluation, fee increase).

Deterministic: a post qualifies when its title names a tracked card AND has a
change keyword. Each post is considered once (its URL is remembered). News
normally comes from the collector's snapshot on the data branch, so this job
usually makes no request of its own.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from card_agent.collector import doc_rss
from card_agent.matching import CardMatcher
from card_agent.models import Card, NewsItem, Snapshot
from card_agent.terms.sources import CardSource
from card_agent.terms.state import QueueEntry, TermsQueue

CHANGE_WORDS = re.compile(
    r"\bchang(?:e|es|ed|ing)\b"
    r"|\brefresh(?:es|ed|ing)?\b"
    r"|\bnew benefits?\b"
    r"|\bdevalu(?:ation|ations|e|es|ed|ing)\b"
    r"|\bincreas(?:e|es|ed|ing)\b"
    r"|\bannual fees?\b",
    re.I,
)
MAX_SEEN = 500
QUEUE_MAX_AGE_DAYS = 35
NEWS_MAX_AGE_DAYS = 8


def tracked_matcher(sources: dict[str, CardSource]) -> CardMatcher:
    """A matcher over the names each tracked card goes by."""
    cards = [
        Card(id=source.card_id, issuer=source.issuer, name=name)
        for source in sources.values()
        if not source.is_manual
        for name in [*source.page_names, source.name]
    ]
    return CardMatcher(cards)


def is_change_post(title: str) -> bool:
    return bool(CHANGE_WORDS.search(title))


def scan(
    news: list[NewsItem], sources: dict[str, CardSource], queue: TermsQueue, today: date
) -> list[str]:
    """Add matching cards to `queue`; return the card ids newly queued."""
    matcher = tracked_matcher(sources)
    seen = set(queue.seen_posts)
    added: list[str] = []
    for item in news:
        if item.url in seen:
            continue
        seen.add(item.url)
        queue.seen_posts.append(item.url)
        if not is_change_post(item.title):
            continue
        for card_id in matcher.find(item.title):
            if card_id not in queue.queued:
                added.append(card_id)
            queue.queued[card_id] = QueueEntry(reason=item.title, url=item.url, queued_at=today)
    queue.seen_posts = queue.seen_posts[-MAX_SEEN:]
    cutoff = today - timedelta(days=QUEUE_MAX_AGE_DAYS)
    queue.queued = {cid: e for cid, e in queue.queued.items() if e.queued_at >= cutoff}
    return added


def load_news(
    data_dir: Path, client: httpx.Client, sources: dict[str, CardSource], now: datetime
) -> tuple[list[NewsItem], str]:
    """The collector's news if it's fresh, else one fetch of the feed's first page."""
    latest = data_dir / "latest.json"
    if latest.exists():
        snapshot = Snapshot.model_validate_json(latest.read_text())
        if now - snapshot.generated_at <= timedelta(days=NEWS_MAX_AGE_DAYS):
            return snapshot.news, f"collector snapshot of {snapshot.generated_at:%Y-%m-%d}"
    response = client.get(doc_rss.FEED_URL)
    response.raise_for_status()
    return doc_rss.parse_feed(response.content, tracked_matcher(sources)), doc_rss.FEED_URL
