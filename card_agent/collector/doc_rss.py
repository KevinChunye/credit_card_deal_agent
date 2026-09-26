"""Source 2: Doctor of Credit's WordPress RSS feed.

The credit-card category feed (/category/credit-cards/feed/) returns ~15
posts, which is about a week of posts. We keep a rolling 35-day window by
merging each run's new posts into the previous snapshot's news, and page back
(WordPress `?paged=N`) only as far as needed: at most `max_pages` requests.

Rule-based classification, no LLM: a post is a `new_bonus` or
`elevated_bonus` when its title names an amount and bonus words; card ids are
linked with the shared CardMatcher. Everything else is kept as `news`.
Links in posts are stored, never followed.
"""

from __future__ import annotations

import re
from calendar import timegm
from datetime import UTC, datetime, timedelta

import feedparser
import httpx

from card_agent.matching import CardMatcher
from card_agent.models import NewsItem

FEED_URL = "https://www.doctorofcredit.com/category/credit-cards/feed/"
WINDOW_DAYS = 35

ELEVATED_WORDS = re.compile(
    r"\belevated\b|\bincreased?\b|\bhigher\b|\bbest (?:ever|offer)\b|all[- ]time high|"
    r"\bup to\b|\blimited[- ]time\b|\bis back\b|\breturns\b|\bhighest\b",
    re.I,
)
BONUS_WORDS = re.compile(r"\bbonus\b|\bsign[- ]?up\b|\bwelcome offer\b|\boffer\b", re.I)
AMOUNT = re.compile(
    r"(?P<pts>\d{1,3}(?:,\d{3})+|\d{2,3}k)\s*(?:bonus\s+)?(?:points|miles|pts|avios|bonus)"
    r"|\$(?P<usd>\d{2,4}(?:,\d{3})?)\s*(?:cash\s*)?(?:bonus|back|statement credit|sign)",
    re.I,
)
BANK_WORDS = re.compile(r"\bchecking\b|\bsavings\b|\bbank account\b|\bbrokerage\b", re.I)
EXPIRED = re.compile(r"^\s*[\[(](?:expired|dead)[\])]", re.I)


def parse_amount(title: str) -> float | None:
    match = AMOUNT.search(title)
    if not match:
        return None
    if match.group("pts"):
        raw = match.group("pts").lower()
        return float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw.replace(",", ""))
    return float(match.group("usd").replace(",", ""))


def classify(title: str, tags: list[str]) -> tuple[str, float | None, list[str]]:
    """(kind, amount, extra_tags) for a post title."""
    extra: list[str] = []
    lowered_tags = {tag.lower() for tag in tags}
    if EXPIRED.search(title):
        extra.append("expired")
    if BANK_WORDS.search(title) or "bank accounts" in lowered_tags:
        extra.append("bank")
    amount = parse_amount(title)
    is_card = "credit cards" in lowered_tags or re.search(r"\bcard\b", title, re.I)
    if "expired" in extra or "bank" in extra or not is_card:
        return "news", amount, extra
    if amount is not None and BONUS_WORDS.search(title):
        kind = "elevated_bonus" if ELEVATED_WORDS.search(title) else "new_bonus"
        return kind, amount, extra
    return "news", amount, extra


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=UTC)


def _summary(entry) -> str | None:
    raw = entry.get("summary") or ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:280] or None


def parse_feed(content: bytes, matcher: CardMatcher) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        published = _published(entry)
        title = (entry.get("title") or "").strip()
        link = entry.get("link")
        if not (published and title and link):
            continue
        tags = [tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")]
        kind, amount, extra_tags = classify(title, tags)
        items.append(
            NewsItem(
                title=title,
                url=link,
                published_at=published,
                kind=kind,
                tags=[*tags, *extra_tags],
                card_ids=matcher.find(title),
                amount=amount,
                summary=_summary(entry),
            )
        )
    return items


def fetch_news(
    client: httpx.Client,
    matcher: CardMatcher,
    now: datetime,
    previous: list[NewsItem] | None = None,
    feed_url: str = FEED_URL,
    max_pages: int = 6,
) -> tuple[list[NewsItem], int]:
    """New posts merged with `previous`, trimmed to the last 35 days.

    Returns (items, pages_fetched). Pages back only until it reaches posts
    older than both the window start and the newest post we already have.
    """
    previous = previous or []
    cutoff = now - timedelta(days=WINDOW_DAYS)
    newest_known = max((item.published_at for item in previous), default=cutoff)
    stop_at = max(cutoff, newest_known)

    fetched: list[NewsItem] = []
    pages = 0
    for page in range(1, max_pages + 1):
        url = feed_url if page == 1 else f"{feed_url}?paged={page}"
        response = client.get(url)
        pages += 1
        if response.status_code == 404 and page > 1:
            break  # past the last page
        response.raise_for_status()
        items = parse_feed(response.content, matcher)
        if not items:
            break
        fetched.extend(items)
        if min(item.published_at for item in items) <= stop_at:
            break

    merged = {item.url: item for item in previous}
    merged.update({item.url: item for item in fetched})  # fresh copies win
    kept = [item for item in merged.values() if item.published_at >= cutoff]
    kept.sort(key=_published_at, reverse=True)
    return kept, pages


def _published_at(item: NewsItem) -> datetime:
    return item.published_at
