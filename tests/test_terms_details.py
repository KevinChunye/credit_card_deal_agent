"""config/card_details.yaml as a generated artifact, and how the collector uses
the pipeline's output (benefit merge, provenance stamps)."""

from __future__ import annotations

from datetime import date

from card_agent.collector.run import stamp_provenance
from card_agent.collector.seed import apply_seed, merge_benefits
from card_agent.models import Benefit, BenefitKind, Card
from card_agent.terms.details import (
    DETAILS_PATH,
    apply_terms,
    diff_terms,
    dump_details,
    earn_to_yaml,
    load_details,
    terms_from_entry,
)
from card_agent.terms.schema import BenefitRow, CardTerms
from card_agent.terms.sources import load_sources
from card_agent.terms.state import CardState, PageHashes, StateFiles

CASH_PLUS = {
    "annual_fee": 0,
    "point_currency": "usd",
    "earn": [
        {
            "choice": ["streaming", "ev_charging", "drugstores"],
            "choose": 2,
            "multiplier": 5,
            "cap": 2000,
            "cap_period": "quarter",
        },
        {"choice": ["groceries", "gas"], "choose": 1, "multiplier": 2},
        {"category": "other", "multiplier": 1},
    ],
    "override": {"point_currency": "usd", "network": "visa"},
    "protections": ["cell_phone"],
}


def test_the_checked_in_yaml_is_in_generated_form():
    """Round trip is byte-identical, so pipeline PRs only show real changes."""
    assert dump_details(load_details()) == DETAILS_PATH.read_text()


def test_choice_menus_round_trip():
    terms = terms_from_entry(CASH_PLUS)
    menus = {row.choice_group for row in terms.earn if row.choice_group}
    assert len(menus) == 2
    assert earn_to_yaml(terms.earn) == [
        {"category": "other", "multiplier": 1.0},
        {
            "choice": ["streaming", "ev_charging", "drugstores"],
            "choose": 2,
            "multiplier": 5.0,
            "cap": 2000,
            "cap_period": "quarter",
        },
        {"choice": ["groceries", "gas"], "choose": 1, "multiplier": 2.0},
    ]


def test_apply_terms_replaces_generated_fields_only():
    terms = terms_from_entry(CASH_PLUS)
    terms.point_currency = "usd"
    terms.evidence = {"annual_fee": "No annual fee"}
    applied = apply_terms(
        CASH_PLUS, terms, "https://bank.example/cash", date(2026, 10, 3), "gpt-6-luna"
    )

    assert applied["protections"] == ["cell_phone"]
    assert applied["override"] == {"network": "visa"}  # point_currency moved to the top level
    assert applied["point_currency"] == "usd"
    assert applied["evidence"] == {"annual_fee": "No annual fee"}
    assert applied["terms_source"]["method"] == "llm:gpt-6-luna"
    assert CASH_PLUS["override"]["point_currency"] == "usd"  # the input is untouched


def test_diff_ignores_rewording_and_reports_real_changes():
    old = terms_from_entry(CASH_PLUS)
    old.benefits = [
        BenefitRow(kind=BenefitKind.streaming_credit, name="Streaming credit", amount=60)
    ]
    new = old.model_copy(deep=True)
    new.benefits = [
        BenefitRow(kind=BenefitKind.streaming_credit, name="Monthly streaming credit", amount=60)
    ]
    assert diff_terms("us-bank-cash", old, new) == []

    new.earn = [
        row.model_copy(update={"multiplier": 6.0}) if row.choose == 2 else row for row in new.earn
    ]
    new.annual_fee = 95
    rows = {(r.field, r.change): (r.old, r.new) for r in diff_terms("us-bank-cash", old, new)}
    assert rows[("annual_fee", "changed")] == ("$0", "$95")
    menu = "earn.choice(drugstores, ev_charging, streaming)"
    assert rows[(menu, "changed")] == (
        "5x on 2 of the menu up to $2,000/quarter",
        "6x on 2 of the menu up to $2,000/quarter",
    )


def test_missing_fields_are_not_diffed():
    old = terms_from_entry(CASH_PLUS)
    assert diff_terms("us-bank-cash", old, CardTerms()) == []


def benefit(kind: str, value: float, source: str) -> Benefit:
    return Benefit(
        card_id="amex-gold",
        kind=BenefitKind(kind),
        name=kind,
        face_value_annual=value,
        source=source,
    )


def test_issuer_page_credits_replace_api_credits_kind_by_kind():
    api = [
        benefit("dining_credit", 84, "api"),
        benefit("rideshare_credit", 120, "api"),
        benefit("lounge", 50, "api"),
    ]
    seed = [
        benefit("dining_credit", 120, "seed"),  # valued: replaces the API's dining credit
        benefit("lounge", 0, "seed"),  # unvalued: the API's valuation stays
        benefit("elite_status", 0, "seed"),  # unvalued, API has none: kept
    ]
    merged = {(b.kind.value, b.source): b.face_value_annual for b in merge_benefits(api, seed)}
    assert merged == {
        ("dining_credit", "seed"): 120,
        ("rideshare_credit", "api"): 120,
        ("lounge", "api"): 50,
        ("elite_status", "seed"): 0,
    }


def test_collector_stamps_terms_provenance(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "card_sources.yaml").write_text(
        "cards:\n"
        "  amex-gold: {issuer: amex, name: Gold, url: 'https://bank.example/gold'}\n"
        "  chase-sapphire-preferred: {issuer: chase, name: CSP, url: 'https://bank.example/csp'}\n"
        "  citi-custom-cash: {issuer: citi, name: Custom Cash, url: null, manual_reason: JS}\n"
    )
    data = tmp_path / "data"
    files = StateFiles(data)
    files.save(
        PageHashes(
            cards={
                "amex-gold": CardState(
                    url="https://bank.example/gold",
                    sha256="abc",
                    last_verified=date(2026, 10, 1),
                    source_status="ok",
                    extraction_status="ok",
                ),
                "chase-sapphire-preferred": CardState(
                    url="https://bank.example/csp",
                    last_verified=date(2026, 6, 1),
                    source_status="ok",
                    extraction_status="ok",
                ),
            }
        ),
        files.hashes_path,
    )
    cards = [
        Card(id="amex-gold", issuer="amex", name="Gold"),
        Card(id="chase-sapphire-preferred", issuer="chase", name="Sapphire Preferred"),
        Card(id="citi-custom-cash", issuer="citi", name="Custom Cash"),
        Card(id="discover-it", issuer="discover", name="Discover it"),
    ]
    stamped, status = stamp_provenance(cards, config, data, date(2026, 10, 3))
    by_id = {card.id: card for card in stamped}

    gold = by_id["amex-gold"]
    assert (gold.terms_tracked, gold.source_status, gold.last_verified) == (
        True,
        "ok",
        date(2026, 10, 1),
    )
    assert gold.source_url == "https://bank.example/gold"
    assert by_id["citi-custom-cash"].source_status == "manual"
    assert by_id["discover-it"].terms_tracked is False
    # Verified in June: more than 60 days ago, so stale.
    assert status.detail == "1 of 3 tracked cards verified in the last 60 days"


def test_live_config_is_structurally_valid():
    """The live files are regenerated by the pipeline, so only structure is
    asserted here (the workflow runs this before opening a terms PR)."""
    details = load_details()["cards"]
    tracked = load_sources()
    assert set(tracked) <= set(details), "every tracked card needs a card_details entry"
    for card_id, source in tracked.items():
        if source.is_manual:
            assert source.manual_reason, f"{card_id}: manual cards say why"
        else:
            assert source.url.startswith("https://") and source.page_names, card_id
    for card_id, entry in details.items():
        if (entry.get("override") or {}).get("discontinued"):
            assert card_id not in tracked, f"{card_id}: discontinued cards aren't tracked"
            assert entry.get("notes"), f"{card_id}: say why it's discontinued"
        terms = terms_from_entry(entry)  # raises on an unknown category, kind, or cadence
        if terms.annual_fee is not None:
            assert 0 <= terms.annual_fee <= 1000, card_id
        for row in terms.earn or []:
            assert 0.5 <= row.multiplier <= 15, (card_id, row)
        for row in terms.benefits or []:
            assert row.amount is None or 0 <= row.amount <= 2000, (card_id, row)


def test_discontinued_override_reaches_the_card():
    seeded = apply_seed(
        {"cards": {"citi-custom-cash": {"override": {"discontinued": True}, "notes": "closed"}}},
        [Card(id="citi-custom-cash", issuer="citi", name="Custom Cash")],
        [],
    )
    assert seeded.cards[0].discontinued is True
