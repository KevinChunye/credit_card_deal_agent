import json
from datetime import timedelta

from card_agent.collector import bonuses_api, doc_rss
from card_agent.collector.run import run_collector, summary_markdown, write_outputs
from card_agent.models import Snapshot
from tests.conftest import CONFIG_FIXTURE, NOW, fixture_path, routed_client


def routes(api_raw, feed_pages):
    return {
        bonuses_api.DATA_URL: json.dumps(api_raw),
        doc_rss.FEED_URL: feed_pages[0],
        f"{doc_rss.FEED_URL}?paged=2": feed_pages[1],
    }


def test_end_to_end_run_writes_three_files(tmp_path, api_raw, feed_pages):
    client, seen = routed_client(routes(api_raw, feed_pages))
    snapshot, changes = run_collector(
        tmp_path, client, NOW, with_issuer_pages=False, config_dir=CONFIG_FIXTURE
    )
    paths = write_outputs(tmp_path, snapshot, changes)

    assert paths["latest"] == tmp_path / "data" / "latest.json"
    assert paths["snapshot"].name == "2026-09-26.json"
    assert paths["changes"].parent.name == "changes"
    loaded = Snapshot.model_validate_json(paths["latest"].read_text())
    assert loaded.changes_file == "data/changes/2026-09-26.json"
    assert loaded.sources["bonuses_api"].status == "ok"
    assert loaded.sources["rewards_db"].status == "disabled"
    assert loaded.sources["doc_rss"].status == "ok"
    assert changes.previous_date is None and changes.is_empty  # first run is the baseline
    # The bonuses API is fetched exactly once.
    assert seen.count(bonuses_api.DATA_URL) == 1

    # Seed applied: curated rates replace the flat base rate, and extra cards exist.
    csp_rates = {
        r.category.value: r.multiplier
        for r in loaded.earn_rates
        if r.card_id == "chase-sapphire-preferred"
    }
    assert csp_rates["dining"] == 3 and csp_rates["travel_portal"] == 5
    assert {"apple-card", "robinhood-gold-card"} <= {c.id for c in loaded.cards}
    assert loaded.downgrade_paths["chase-sapphire-preferred"][0] == "chase-freedom-unlimited"
    custom = [r for r in loaded.earn_rates if r.card_id == "citi-custom-cash" and r.choice_group]
    assert custom and all(r.choose == 1 for r in custom)
    assert "| bonuses_api | ok |" in summary_markdown(loaded, changes)


def test_rss_failure_keeps_previous_news(tmp_path, api_raw, feed_pages):
    client, _ = routed_client(routes(api_raw, feed_pages))
    first, changes = run_collector(
        tmp_path, client, NOW, with_issuer_pages=False, config_dir=CONFIG_FIXTURE
    )
    write_outputs(tmp_path, first, changes)

    broken, _ = routed_client({bonuses_api.DATA_URL: json.dumps(api_raw)})  # feed 404s
    later = NOW + timedelta(days=7)
    second, _ = run_collector(
        tmp_path, broken, later, with_issuer_pages=False, config_dir=CONFIG_FIXTURE
    )
    assert second.sources["doc_rss"].status == "error"
    assert [n.url for n in second.news] == [n.url for n in first.news]


def test_rewards_db_is_opt_in_and_fills_gaps(tmp_path, api_raw, feed_pages):
    client, _ = routed_client(routes(api_raw, feed_pages))
    snapshot, _ = run_collector(
        tmp_path,
        client,
        NOW,
        rewards_dir=fixture_path("rewards_db"),
        with_rss=False,
        with_issuer_pages=False,
        config_dir=CONFIG_FIXTURE,
    )
    assert snapshot.sources["rewards_db"].status == "ok"
    assert snapshot.sources["rewards_db"].count == 1
    blue = [r for r in snapshot.earn_rates if r.card_id == "amex-delta-skymiles-blue"]
    assert {r.category.value for r in blue} == {"dining", "other"}  # delta_purchases skipped
    assert all(r.source == "rewards_db" for r in blue)
    protections = {
        p.kind.value for p in snapshot.protections if p.card_id == "amex-delta-skymiles-blue"
    }
    assert protections == {"secondary_rental", "purchase"}
    credit = [b for b in snapshot.benefits if b.card_id == "amex-delta-skymiles-blue"]
    assert credit[0].kind.value == "airline_fee" and credit[0].face_value_annual == 60
    card = {c.id: c for c in snapshot.cards}["amex-delta-skymiles-blue"]
    assert card.foreign_tx_fee is False


def test_issuer_pages_only_cross_check_true(tmp_path, api_raw):
    page_html = fixture_path("issuer_pages/chase_sapphire_preferred.html").read_text()
    config = tmp_path / "config"
    config.mkdir()
    (config / "card_details.yaml").write_text((CONFIG_FIXTURE / "card_details.yaml").read_text())
    (config / "card_sources.yaml").write_text(
        "cards:\n"
        "  chase-sapphire-preferred: {issuer: chase, name: CSP, url: 'https://bank.example/csp', cross_check: true}\n"
        "  amex-gold: {issuer: amex, name: Gold, url: 'https://bank.example/gold', cross_check: false}\n"
    )
    client, seen = routed_client(
        {bonuses_api.DATA_URL: json.dumps(api_raw), "https://bank.example/csp": page_html}
    )
    snapshot, _ = run_collector(tmp_path, client, NOW, with_rss=False, config_dir=config)
    assert "https://bank.example/gold" not in seen
    checks = {c["field"]: c for c in snapshot.cross_checks}
    assert checks["annual_fee"]["match"] is True
    assert checks["bonus_amount"]["match"] is True


def test_second_run_diffs_against_previous(tmp_path, api_raw):
    first, changes = run_collector(
        tmp_path,
        None,
        NOW,
        source_file=fixture_path("bonuses_api_data.json"),
        with_rss=False,
        with_issuer_pages=False,
        config_dir=CONFIG_FIXTURE,
    )
    write_outputs(tmp_path, first, changes)

    changed = [dict(card) for card in api_raw if card["name"] != "Green"]  # removed card
    for card in changed:
        if card["name"] == "Sapphire Preferred":
            card["offers"] = [dict(card["offers"][0], amount=[{"amount": 100000}])]  # elevated
        if card["name"] == "Venture X":
            card["offers"] = [dict(card["offers"][0], amount=[{"amount": 60000}])]  # reduced
        if card["name"] == "Gold" and card["issuer"] == "AMERICAN_EXPRESS":
            card["annualFee"] = 395  # fee change
        if card["name"] == "Freedom Flex":
            card["offers"] = []  # removed offer
    template = [card for card in changed if card["issuer"] == "CHASE"][0]
    changed.append(dict(template, cardId="new123", name="Sapphire Horizon", offers=[]))
    export = tmp_path / "week2.json"
    export.write_text(json.dumps(changed))

    later = NOW + timedelta(days=7)
    _, changes = run_collector(
        tmp_path,
        None,
        later,
        source_file=export,
        with_rss=False,
        with_issuer_pages=False,
        config_dir=CONFIG_FIXTURE,
    )
    assert changes.previous_date == NOW.date()
    assert [c["card_id"] for c in changes.new_cards] == ["chase-sapphire-horizon"]
    assert [c["card_id"] for c in changes.removed_cards] == ["amex-green"]
    assert [(c["card_id"], c["new_bonus"]) for c in changes.elevated_bonuses] == [
        ("chase-sapphire-preferred", 100000)
    ]
    assert [c["card_id"] for c in changes.reduced_bonuses] == ["capital-one-venture-x"]
    assert changes.fee_changes == [
        {"card_id": "amex-gold", "name": "Gold", "old_annual_fee": 350.0, "new_annual_fee": 395.0}
    ]
    assert [c["card_id"] for c in changes.removed_offers] == ["chase-freedom-flex"]
