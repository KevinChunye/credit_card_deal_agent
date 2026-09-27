"""Chat formatting helpers, the official-link policy, and the credit check."""

from __future__ import annotations

from datetime import date

import pytest

from card_agent.credit import assess
from card_agent.links import apply_link, load_links, on_issuer_domain
from card_agent.models import Card, Category, Snapshot, UserProfile, WalletCard
from card_agent.present import bar, bar_lines, markup_leaks, parse_category
from card_agent.scoring import ScoringContext
from card_agent.snapshot import DataView
from tests.conftest import NOW

TODAY = date(2026, 9, 26)


# ------------------------------------------------------------------ present
def test_bars_scale_from_zero():
    assert bar(10, 10) == "█" * 10
    assert bar(5, 10) == "█████░░░░░"
    assert bar(0.1, 10) == "█" + "░" * 9  # anything positive shows
    assert bar(-50, 10) == bar(0, 10) == "░" * 10
    lines = bar_lines([(300.0, "+$300 A"), (150.0, "+$150 B"), (-20.0, "−$20 C")])
    assert lines == ["██████████ +$300 A", "█████░░░░░ +$150 B", "░░░░░░░░░░ −$20 C"]


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("```\ntable\n```", "code fence"),
        ("run `sync` first", "backtick"),
        ("**Top pick**", "bold markup"),
        ("## Data health", "heading markup"),
        ("[Doctor of Credit](https://example.com)", "link markup"),
        ("*Card digest*", "asterisk emphasis"),
        ("_No changes._", "underscore emphasis"),
        ('say "explain <card>"', "html tag"),
        ('{"ok": true}', "json"),
    ],
)
def test_markup_leaks_are_caught(text, leak):
    assert leak in markup_leaks(text)


def test_plain_chat_text_is_clean():
    text = (
        "🏆 My pick for you: Chase Sapphire Preferred ($95 fee)\n"
        "██████░░░░ +$1,234 year 1 · −$37/yr after\n"
        "🔗 https://creditcards.chase.com/rewards-credit-cards/sapphire/preferred\n"
        "5/24 status: 3 of 5 · chase_ur points × 1.5¢"
    )
    assert markup_leaks(text) == []


@pytest.mark.parametrize(
    ("words", "category"),
    [
        ("Uber", Category.transit_rideshare),
        ("eating out", Category.dining),
        ("Groceries", Category.groceries),
        ("online groceries", Category.online_groceries),
        ("travel_portal", Category.travel_portal),
        ("everything else", Category.other),
        ("bowling", None),
    ],
)
def test_parse_category(words, category):
    assert parse_category(words) == category


# -------------------------------------------------------------------- links
def test_official_links_are_https_and_on_the_issuers_domain():
    links = load_links()
    assert set(links.prequalify) >= {"chase", "amex", "capital-one", "citi"}
    for issuer, url in links.prequalify.items():
        assert on_issuer_domain(url, issuer), (issuer, url)
    assert links.credit_resources
    assert all(url.startswith("https://") for _, url in links.credit_resources)


def test_apply_link_policy():
    curated = "https://creditcards.chase.com/rewards-credit-cards/sapphire/preferred"
    card = Card(id="c", issuer="chase", name="Sapphire Preferred", source_url=curated)
    assert apply_link(card) == curated
    # The feed's link is used only when it's on the issuer's own domain, over https.
    feed = Card(id="c", issuer="chase", name="X", apply_url="https://creditcards.chase.com/x")
    assert apply_link(feed) == "https://creditcards.chase.com/x"
    for url in (
        "https://www.nerdwallet.com/chase-x",  # third party
        "https://chase.com.evil.example/x",  # lookalike
        "http://creditcards.chase.com/x",  # not https
    ):
        assert apply_link(Card(id="c", issuer="chase", name="X", apply_url=url)) is None
    assert apply_link(card.model_copy(update={"discontinued": True})) is None


# ------------------------------------------------------------------- credit
def _context(wallet: list[WalletCard], profile: UserProfile, spend: float) -> ScoringContext:
    cards = [
        Card(id=f"c{i}", issuer="chase", name=f"Card {i}", point_currency="usd") for i in range(8)
    ] + [Card(id="biz", issuer="amex", name="Biz", is_business=True)]
    return ScoringContext(
        data=DataView(Snapshot(generated_at=NOW, cards=cards)),
        profile=profile,
        spend={Category.other: spend},
        valuations={},
        haircuts={},
        wallet=wallet,
        rules=[],
        today=TODAY,
    )


def test_credit_check_counts_524_and_says_when_it_frees_up():
    opened = [
        date(2024, 11, 1),
        date(2025, 2, 1),
        date(2025, 5, 1),
        date(2025, 8, 1),
        date(2026, 1, 1),
        date(2026, 8, 20),
    ]
    wallet = [WalletCard(card_id=f"c{i}", opened_on=day) for i, day in enumerate(opened)]
    wallet += [
        WalletCard(card_id="c6", opened_on=date(2012, 3, 1)),  # oldest, outside 24 months
        WalletCard(card_id="biz", opened_on=date(2026, 3, 1)),  # Amex business: not counted
        WalletCard(card_id="c7"),  # no date
    ]
    health = assess(_context(wallet, UserProfile(credit_score_band="building"), 900))
    assert health.chase_524 == 6 and health.new_last_12 == 3 and health.undated == 1
    notes = "\n".join(health.notes)
    # Six counted: back under five once the two oldest age out (Feb 2025 + 24 months).
    assert "Chase will likely decline new cards until about Feb 2027" in notes
    assert "⏳ Your newest card (Chase Card 5) is 37 days old" in notes
    assert "🏛️ Your oldest open card is Chase Card 6 (15 years, since Mar 2012)" in notes
    assert "📝 1 card in your wallet has no open date" in notes
    assert "💡 Tell me your total credit limit" in notes
    assert "🎯 Score range building (no score yet, or under 580)" in notes


@pytest.mark.parametrize(
    ("limit", "icon"),
    [(2000.0, "⚠️"), (6000.0, "👍"), (20000.0, "💪")],
)
def test_credit_utilization_bands(limit, icon):
    wallet = [WalletCard(card_id="c0", opened_on=date(2020, 1, 1))]
    health = assess(_context(wallet, UserProfile(total_credit_limit=limit), 900))
    assert health.utilization == pytest.approx(900 / limit)
    assert any(note.startswith(f"{icon} If your statements show") for note in health.notes)
