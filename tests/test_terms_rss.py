"""The RSS trigger: a Doctor of Credit post queues a card for re-extraction when
its title names a tracked card together with a change keyword."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from card_agent.collector import doc_rss
from card_agent.models import NewsItem
from card_agent.terms.rss_trigger import is_change_post, load_news, scan
from card_agent.terms.sources import CardSource
from card_agent.terms.state import QueueEntry, TermsQueue
from tests.conftest import fixture_path, routed_client
from tests.terms_fakes import TODAY, sources

POSTED = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)


def tracked() -> dict[str, CardSource]:
    cards = sources()
    cards["amex-delta-skymiles-gold"] = CardSource(
        card_id="amex-delta-skymiles-gold",
        issuer="amex",
        name="Delta SkyMiles Gold American Express Card",
        url="https://www.americanexpress.com/us/credit-cards/card/delta-skymiles-gold-american-express-card/",
        page_names=["Delta SkyMiles Gold"],
    )
    return cards


def post(title: str, slug: str) -> NewsItem:
    return NewsItem(title=title, url=f"https://www.doctorofcredit.com/{slug}/", published_at=POSTED)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Chase Sapphire Preferred Changes Coming In 2027", True),
        ("Amex Gold Card Refresh: New Benefits", True),
        ("Capital One Venture X Devaluation", True),
        ("Delta Amex Annual Fee Increase", True),
        ("Amex Gold Adds New Benefit For Cardmembers", True),
        ("Chase Sapphire Preferred 100,000 Points Signup Bonus", False),
        ("Exchange Rates And Your Card", False),
    ],
)
def test_change_keywords(title, expected):
    assert is_change_post(title) is expected


def test_scan_queues_tracked_cards_named_in_change_posts():
    queue = TermsQueue()
    news = [
        post("Chase Sapphire Preferred Changes Coming In 2027", "csp-changes"),
        post("Chase Sapphire Preferred 100,000 Points Signup Bonus", "csp-100k"),
        post("Amex Gold Card Annual Fee Increasing To $350", "gold-fee"),
        # "Gold" alone must not match the Amex Gold when the post is about Delta Gold.
        post("Delta SkyMiles Gold Card Refresh", "delta-gold-refresh"),
        # Manual cards aren't tracked, so there is nothing to re-extract.
        post("Citi Custom Cash Devaluation", "custom-cash"),
        post("Bank Of America Changes Checking Fees", "boa-checking"),
    ]
    added = scan(news, tracked(), queue, TODAY)

    assert added == ["chase-sapphire-preferred", "amex-gold", "amex-delta-skymiles-gold"]
    assert queue.queued["amex-gold"].reason == "Amex Gold Card Annual Fee Increasing To $350"
    assert queue.queued["amex-gold"].url == "https://www.doctorofcredit.com/gold-fee/"
    assert len(queue.seen_posts) == len(news)

    # The same posts again: nothing new.
    assert scan(news, tracked(), queue, TODAY + timedelta(days=7)) == []


def test_old_queue_entries_expire():
    queue = TermsQueue(
        queued={
            "amex-gold": QueueEntry(
                reason="old", url="https://doc.example/old", queued_at=TODAY - timedelta(days=60)
            )
        }
    )
    scan([], tracked(), queue, TODAY)
    assert queue.queued == {}


def test_load_news_fetches_the_feed_when_there_is_no_fresh_snapshot(tmp_path):
    feed = fixture_path("doc_feed_page1.xml").read_bytes()
    client, seen = routed_client({doc_rss.FEED_URL: feed})
    news, origin = load_news(tmp_path, client, tracked(), datetime.now(UTC))
    assert origin == doc_rss.FEED_URL and seen == [doc_rss.FEED_URL]
    assert news and all(item.title for item in news)
