"""The private state keeps its integrity: updates don't erase what they don't
mention, a bad setup saves nothing, and timestamps compare as times."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from card_agent import cli, onboard, trace
from card_agent.config import REPO_ROOT
from card_agent.models import Category, PersonalOffer
from card_agent.store import SCHEMA_VERSION, Store
from tests.conftest import NOW, run, setup_profile


def test_updating_one_wallet_date_keeps_the_others(capsys, env):
    setup_profile(capsys, env)
    # The example profile has Sapphire Preferred opened 2024-03-15 with its bonus.
    code, out = run(capsys, "wallet", "add", "chase-sapphire-preferred", "--fee-date", "2026-06-01")
    assert code == 0
    assert out["card"] == {
        "card_id": "chase-sapphire-preferred",
        "opened_on": "2024-03-15",
        "annual_fee_date": "2026-06-01",
        "bonus_received_on": "2024-06-01",
        "product_changed_from": None,
        "closed_on": None,
    }
    # Same through onboard's wallet section.
    run(
        capsys,
        "onboard",
        "--json",
        '{"wallet": [{"card_id": "amex gold", "closed_on": "2026-09-01"}]}',
    )
    code, out = run(capsys, "wallet", "list")
    gold = next(w for w in out["wallet"] if w["card_id"] == "amex-gold")
    assert gold["opened_on"] == "2025-01-10" and gold["closed_on"] == "2026-09-01"


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"monthly_spend": {"dining": 999}, "haircuts": {"lounge": 7}}, "0.0 to 1.0"),
        ({"monthly_spend": {"dining": 999, "groceries": -5}}, "can't be negative"),
        ({"monthly_spend": {"dining": 999, "bowling": 50}}, "isn't a valid spend category"),
        ({"monthly_spend": {"dining": 999}, "wallet": [{"opened_on": "2024-01-01"}]}, "card_id"),
        ({"profile": {"max_annual_fee": 50}, "valuations": {"chase_ur": 0}}, "positive"),
    ],
)
def test_a_bad_setup_saves_nothing(capsys, env, patch, error):
    setup_profile(capsys, env)
    code, out = run(capsys, "onboard", "--json", json.dumps(patch))
    assert code == 1 and out["ok"] is False and error in out["error"]
    code, out = run(capsys, "onboard", "--show")
    # Nothing from the rejected update landed, not even the valid parts.
    assert out["setup"]["monthly_spend"]["dining"] == 600
    assert out["setup"]["profile"]["max_annual_fee"] == 700


def test_setup_that_is_not_an_object_is_a_clear_error(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "onboard", "--json", '["dining", 600]')
    assert code == 1 and "JSON object" in out["error"]


def test_transaction_rolls_back_every_write(tmp_path):
    store = Store(tmp_path / "state.db")
    store.set_spend({Category.dining: 100})
    with pytest.raises(RuntimeError), store.transaction():
        store.set_spend({Category.dining: 500})
        store.set_valuations({"chase_ur": 2.0})
        raise RuntimeError("boom")
    assert store.get_spend() == {Category.dining: 100}
    assert "chase_ur" not in store.custom_valuations()
    store.close()


def _offer(message_id: str, received_at: datetime) -> PersonalOffer:
    return PersonalOffer(message_id=message_id, received_at=received_at, sender="x@chase.com")


def test_offer_dates_compare_as_times_across_timezones(tmp_path):
    store = Store(tmp_path / "state.db")
    eastern = timezone(timedelta(hours=-5))
    # 31 days back, 03:00 UTC is 22:00 the evening before in New York.
    cutoff = NOW - timedelta(days=31)
    store.save_personal_offer(_offer("inside", (cutoff + timedelta(hours=1)).astimezone(eastern)))
    store.save_personal_offer(_offer("outside", (cutoff - timedelta(hours=1)).astimezone(eastern)))
    store.save_personal_offer(_offer("naive", (NOW - timedelta(days=1)).replace(tzinfo=None)))
    found = {offer.message_id for offer in store.personal_offers(since=cutoff)}
    assert found == {"inside", "naive"}
    store.close()


def test_older_databases_are_migrated(tmp_path):
    path = tmp_path / "state.db"
    Store(path).close()
    # Simulate a database written before timestamps were normalized.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 0")
    offer = _offer("legacy", datetime(2026, 9, 20, 20, 0, tzinfo=timezone(timedelta(hours=-7))))
    conn.execute(
        "INSERT INTO personal_offer (message_id, received_at, data) VALUES (?, ?, ?)",
        ("legacy", "2026-09-20T20:00:00-07:00", offer.model_dump_json()),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    row = store.conn.execute("SELECT received_at FROM personal_offer").fetchone()
    assert row["received_at"] == "2026-09-21T03:00:00+00:00"
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    since = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
    assert [o.message_id for o in store.personal_offers(since=since)] == ["legacy"]
    store.close()


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ('{"monthly_spend": {"dining": NaN}}', "isn't a usable number"),
        ('{"monthly_spend": {"dining": Infinity}}', "isn't a usable number"),
        ('{"valuations": {"chase_ur": Infinity}}', "isn't a usable number"),
        ('{"monthly_spend": {"dining": null}}', "isn't a number"),
        ('{"monthly_spend": ["dining"]}', "must be an object"),
        ('{"profile": {"max_anual_fee": 95}}', "Unknown profile field(s): max_anual_fee"),
        ('{"profile": {"trips_per_year": -3}}', "greater than or equal to 0"),
        ('{"wallet": [{"card": "amex gold"}]}', "Unknown wallet field(s): card"),
        ('{"wallet": "amex gold"}', "wallet must be a list"),
        ("null", "must be a JSON object"),
    ],
)
def test_bad_values_are_refused_with_a_clear_error(capsys, env, patch, error):
    setup_profile(capsys, env)
    code, out = run(capsys, "onboard", "--json", patch)
    assert code == 1 and out["ok"] is False and error in out["error"], out
    assert out["display_text"] and out["next"]["action"] == "stop"


def test_unexpected_errors_still_answer_in_json(capsys, env, monkeypatch):
    setup_profile(capsys, env)

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "cmd_memory", locked)
    code, out = run(capsys, "memory")
    assert code == 1 and out["display_text"].startswith("⏳ Your saved data is busy")

    def surprise(*args):
        raise KeyError("boom")

    monkeypatch.setattr(cli, "cmd_memory", surprise)
    code, out = run(capsys, "memory")
    assert code == 1 and out["display_text"] == "⚠️ That didn't work (KeyError: 'boom')."
    code, out = run(capsys, "trace", "--last", "1")
    assert out["records"][-1]["ok"] is False  # and it's in the log


def test_a_broken_database_file_is_reported(capsys, env, tmp_path, monkeypatch):
    bad = tmp_path / "broken" / "state.db"
    bad.parent.mkdir()
    bad.write_text("not a database")
    monkeypatch.setenv("CARD_AGENT_DB", str(bad))
    code, out = run(capsys, "memory")
    assert code == 1 and out["display_text"].startswith("⚠️ I can't open your saved data")
    assert out["next"] == {"action": "stop"}


def test_concurrent_profile_updates_are_not_lost(env):
    path = env.tmp / "state" / "state.db"
    Store(path).close()

    def update(field: str) -> None:
        store = Store(path)
        for i in range(1, 41):
            onboard.apply_patch(store, {"profile": {field: i}}, None)
        store.close()

    threads = [
        threading.Thread(target=update, args=(field,))
        for field in ("trips_per_year", "max_new_cards_per_year")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    profile = Store(path).get_profile()
    assert (profile.trips_per_year, profile.max_new_cards_per_year) == (40, 40)


def test_damaged_card_data_asks_for_a_refresh(capsys, env):
    setup_profile(capsys, env)
    cache = env.tmp / "state" / "cache" / "latest.json"
    cache.write_bytes(cache.read_bytes()[:5000])  # cut off mid-file
    code, out = run(capsys, "rank")
    assert code == 1 and "damaged" in out["error"]
    assert out["next"]["command"] == "sync"
    code, out = run(capsys, "memory")  # memory still works without card data
    assert code == 0


def test_a_torn_trace_line_does_not_break_commands(capsys, env, monkeypatch):
    setup_profile(capsys, env)
    log = env.tmp / "state" / "trace.jsonl"
    with log.open("ab") as handle:
        handle.write(b'{"command": "rank", "summary": "\xf0\x9f')  # cut-off emoji, no newline
    monkeypatch.setattr(trace, "MAX_BYTES", 100)  # force a rotation on the next write
    code, out = run(capsys, "memory")
    assert code == 0
    code, out = run(capsys, "trace", "--last", "2")
    assert code == 0 and [r["command"] for r in out["records"]][-1] == "memory"


def test_hiding_again_keeps_the_reason_and_dates_must_make_sense(capsys, env):
    setup_profile(capsys, env)
    run(capsys, "hide", "--issuer", "chase", "--reason", "too many inquiries at Chase")
    run(capsys, "hide", "--issuer", "chase")
    code, out = run(capsys, "memory")
    assert out["memory"]["hidden"][0]["reason"] == "too many inquiries at Chase"
    code, out = run(capsys, "wallet", "add", "amex-gold", "--closed", "2020-01-01")
    assert code == 1 and "before the open date" in out["error"]


@pytest.mark.parametrize("argv", [["rank", "--top", "0"], ["rank", "--max-af", "nan"]])
def test_nonsense_numbers_are_usage_errors(capsys, env, argv):
    code, out = run(capsys, *argv)
    assert code == 2 and out["next"] == {"action": "fix_command"}


def test_a_relative_state_path_stays_out_of_the_repo(tmp_path):
    env = {**os.environ, "CARD_AGENT_DB": "mine/state.db"}
    result = subprocess.run(
        [str(REPO_ROOT / "bin" / "card-agent"), "memory"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert json.loads(result.stdout)["ok"] is True
    assert (tmp_path / "mine" / "state.db").exists()
    assert not (REPO_ROOT / "mine").exists()
