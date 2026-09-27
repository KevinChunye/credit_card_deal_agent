"""Staleness guard: warning markers in rank/compare/explain/digest and the
digest's data-health line, driven by the card-terms pipeline's provenance."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from card_agent import cli
from card_agent.collector import bonuses_api, doc_rss
from card_agent.collector.run import run_collector, write_outputs
from card_agent.config import CONFIG_DIR
from card_agent.freshness import data_health, terms_warning, verified_label
from card_agent.models import Card
from card_agent.terms.state import CardState, PageHashes, StateFiles
from tests.conftest import CONFIG_FIXTURE, NOW, routed_client

TODAY = NOW.date()
FRESH = TODAY - timedelta(days=6)
OLD = TODAY - timedelta(days=117)


def card(**fields) -> Card:
    return Card(id="x", issuer="chase", name="Test Card", **fields)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "terms not checked against an issuer page"),
        (
            {"terms_tracked": True, "source_status": "ok", "last_verified": FRESH},
            None,
        ),
        (
            {"terms_tracked": True, "source_status": "ok", "last_verified": OLD},
            "terms last verified 2026-06-01 (over 60 days ago)",
        ),
        (
            {"terms_tracked": True, "source_status": "fetch_failed", "last_verified": FRESH},
            "issuer page unreachable; terms last verified 2026-09-20",
        ),
        (
            {"terms_tracked": True, "source_status": "validation_failed"},
            "issuer page didn't pass validation; terms never verified",
        ),
        (
            {"terms_tracked": True, "source_status": "manual"},
            "terms hand-maintained (no readable issuer page)",
        ),
        (
            {"terms_tracked": True, "source_status": "manual", "source_url": "https://x.example"},
            "terms never verified against the issuer page",
        ),
    ],
)
def test_terms_warning(fields, expected):
    assert terms_warning(card(**fields), TODAY) == expected


def test_boundary_is_sixty_days():
    at_limit = card(
        terms_tracked=True, source_status="ok", last_verified=TODAY - timedelta(days=60)
    )
    assert terms_warning(at_limit, TODAY) is None
    assert verified_label(at_limit, TODAY) == "2026-07-28"
    over = card(terms_tracked=True, source_status="ok", last_verified=TODAY - timedelta(days=61))
    assert verified_label(over, TODAY) == "⚠ 2026-07-27"


def test_data_health_counts_tracked_cards_only():
    cards = [
        card(terms_tracked=True, source_status="ok", last_verified=FRESH),
        card(terms_tracked=True, source_status="ok", last_verified=TODAY - timedelta(days=45)),
        card(terms_tracked=True, source_status="ok", last_verified=OLD),
        card(terms_tracked=True, source_status="fetch_failed", last_verified=FRESH),
        card(),  # not tracked: not counted
    ]
    health = data_health(cards, TODAY)
    assert health.tracked == 4
    assert health.verified_this_month == 1
    assert len(health.stale) == 2
    assert health.line == "Data health: 1 card verified this month, 2 stale."


@pytest.fixture
def env(tmp_path, monkeypatch, api_raw, feed_pages):
    """A collector snapshot stamped from page_hashes.json, then an agent DB."""
    out = tmp_path / "data-branch"
    files = StateFiles(out / "data")
    files.save(
        PageHashes(
            cards={
                "capital-one-venture-x": CardState(
                    url="https://www.capitalone.com/credit-cards/venture-x/",
                    source_status="ok",
                    extraction_status="ok",
                    last_verified=FRESH,
                ),
                "chase-sapphire-reserve": CardState(
                    url="https://creditcards.chase.com/rewards-credit-cards/sapphire/reserve",
                    source_status="ok",
                    extraction_status="ok",
                    last_verified=OLD,
                ),
            }
        ),
        files.hashes_path,
    )
    client, _ = routed_client(
        {
            bonuses_api.DATA_URL: json.dumps(api_raw),
            doc_rss.FEED_URL: feed_pages[0],
            f"{doc_rss.FEED_URL}?paged=2": feed_pages[1],
        }
    )
    snapshot, changes = run_collector(
        out, client, NOW, with_issuer_pages=False, config_dir=CONFIG_FIXTURE
    )
    write_outputs(out, snapshot, changes)
    for var in ("AGENTMAIL_API_KEY", "AGENTMAIL_INBOX", "OWNER_EMAIL", "DIGEST_TO_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CARD_AGENT_DB", str(tmp_path / "state" / "state.db"))
    return SimpleNamespace(latest=out / "data" / "latest.json")


def run(capsys, *argv) -> dict:
    code = cli.main(list(argv), now=NOW)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return out


@pytest.fixture
def agent(capsys, env):
    run(capsys, "sync", "--from-file", str(env.latest))
    run(capsys, "onboard", "--from-yaml", str(CONFIG_DIR / "user_profile.example.yaml"))
    return lambda *argv: run(capsys, *argv)


def test_rank_marks_cards_with_unverified_terms(agent):
    out = agent("rank", "--top", "10")
    warnings = {r["card_id"]: r["terms_warning"] for r in out["results"]}
    if "capital-one-venture-x" in warnings:
        assert warnings["capital-one-venture-x"] is None
    flagged = [card_id for card_id, warning in warnings.items() if warning]
    assert flagged  # the fixture leaves most cards unverified
    lines = out["display_text"].splitlines()
    assert any("⚠ terms" in line for line in lines[1:-1])
    assert any(line.startswith("⚠ = terms not verified") for line in lines)


def test_compare_and_explain_show_verification(agent):
    out = agent("compare", "capital-one-venture-x", "chase-sapphire-reserve")
    text = out["display_text"]
    assert re.search(r"Terms verified\s+2026-09-20\s+⚠ 2026-06-01", text)
    assert "⚠ Chase Sapphire Reserve: terms last verified 2026-06-01 (over 60 days ago)." in text
    assert out["a"]["terms_warning"] is None

    out = agent("explain", "capital-one-venture-x")
    assert out["display_text"].endswith(
        "Terms verified 2026-09-20 against https://www.capitalone.com/credit-cards/venture-x/."
    )
    out = agent("explain", "chase-sapphire-reserve")
    assert out["terms_warning"] == "terms last verified 2026-06-01 (over 60 days ago)"


def test_digest_has_a_data_health_line(agent):
    out = agent("digest", "--no-sync")
    short, full = out["display_text"], out["email_markdown"]
    assert re.search(r"Data health: 1 card verified this month, \d+ stale\.", short)
    assert len(short) <= 1500
    assert "## Data health" in full
    assert "- Chase Sapphire Reserve: terms last verified 2026-06-01 (over 60 days ago)" in full
    picks = out["digest"]["sections"]["top_opportunities"]["items"]
    assert all("terms_warning" in pick for pick in picks)
