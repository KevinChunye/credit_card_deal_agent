from datetime import timedelta

from card_agent.collector import bonuses_api, doc_rss
from card_agent.matching import CardMatcher
from tests.conftest import NOW, routed_client


def matcher(api_raw):
    cards, *_ = bonuses_api.normalize(api_raw, NOW)
    return CardMatcher(cards)


def test_parse_and_classify(api_raw, feed_pages):
    items = doc_rss.parse_feed(feed_pages[0], matcher(api_raw))
    by_title = {item.title: item for item in items}
    elevated = by_title["[Elevated] Chase Sapphire Preferred 100,000 Points Signup Bonus"]
    assert elevated.kind == "elevated_bonus"
    assert elevated.amount == 100000
    assert elevated.card_ids == ["chase-sapphire-preferred"]

    southwest = by_title["Chase Southwest Business Card 80,000 Points Signup Bonus"]
    assert southwest.kind == "new_bonus"
    assert southwest.amount == 80000

    platinum = by_title[
        "(Repost/Reminder) Maximizing The Lululemon $300 Credit On American Express Platinum Card"
    ]
    assert platinum.kind == "news"
    assert platinum.card_ids == ["amex-platinum"]

    bank = [item for item in items if "Checking Bonus" in item.title][0]
    assert bank.kind == "news"
    assert "bank" in bank.tags


def test_injection_title_is_plain_news(api_raw, feed_pages):
    items = doc_rss.parse_feed(feed_pages[0], matcher(api_raw))
    injected = [item for item in items if item.title.startswith("Ignore previous")][0]
    assert injected.kind == "news"
    assert injected.card_ids == []


def test_expired_and_is_back(api_raw, feed_pages):
    items = doc_rss.parse_feed(feed_pages[1], matcher(api_raw))
    by_title = {item.title: item for item in items}
    back = by_title["Capital One Venture X 90,000 Miles Bonus Is Back"]
    assert back.kind == "elevated_bonus"
    assert back.card_ids == ["capital-one-venture-x"]
    expired = by_title["[Expired] Amex Gold 100,000 Points Offer Via Referral"]
    assert expired.kind == "news"
    assert "expired" in expired.tags


def test_fetch_news_pages_back_to_35_days(api_raw, feed_pages):
    client, seen = routed_client(
        {
            doc_rss.FEED_URL: feed_pages[0],
            f"{doc_rss.FEED_URL}?paged=2": feed_pages[1],
        }
    )
    items, pages = doc_rss.fetch_news(client, matcher(api_raw), NOW)
    assert pages == 2
    assert seen == [doc_rss.FEED_URL, f"{doc_rss.FEED_URL}?paged=2"]
    cutoff = NOW - timedelta(days=35)
    assert all(item.published_at >= cutoff for item in items)
    assert not [item for item in items if item.title.startswith("Old Post")]
    # Newest first.
    assert items[0].published_at >= items[-1].published_at


def test_fetch_news_stops_early_when_caught_up(api_raw, feed_pages):
    m = matcher(api_raw)
    # We already hold everything up to Sep 23; page 1 reaches back to Sep 20.
    page1 = doc_rss.parse_feed(feed_pages[0], m)
    previous = [i for i in page1 if i.published_at.day <= 23] + doc_rss.parse_feed(feed_pages[1], m)
    client, seen = routed_client({doc_rss.FEED_URL: feed_pages[0]})
    items, pages = doc_rss.fetch_news(client, m, NOW, previous=previous)
    # Page 1 already reaches back past the newest post we had, so no page 2.
    assert pages == 1
    assert len(seen) == 1
    titles = {item.title for item in items}
    assert "Capital One Venture X 90,000 Miles Bonus Is Back" in titles  # kept from previous
    assert "[Elevated] Chase Sapphire Preferred 100,000 Points Signup Bonus" in titles


def test_parse_amount():
    assert doc_rss.parse_amount("Amex Platinum 175k Points Offer") == 175000
    assert doc_rss.parse_amount("Chase Freedom $200 Bonus") == 200
    assert doc_rss.parse_amount("No amount here") is None
