"""The agent layer: the Advisor loop, the Verifier subagent, memory, the trace
log and the `next` instruction. End to end through the CLI on the fixture
snapshot; offline."""

from __future__ import annotations

import json

import pytest

from card_agent import cli, trace
from card_agent.advisor import MAX_CHECKS, Advisor, Goal
from card_agent.config import Settings
from card_agent.present import markup_leaks
from card_agent.store import Store
from card_agent.trace import safe_argv, trace_path
from card_agent.verifier import Brief, Check, Report
from tests.conftest import NOW, run, setup_profile

# Monthly spend low enough that a $4,000-in-90-days bonus is out of reach.
LOW_SPEND = {
    "monthly_spend": {
        "dining": 150,
        "groceries": 250,
        "gas": 60,
        "flights": 0,
        "hotels": 0,
        "transit_rideshare": 0,
        "streaming": 20,
        "drugstores": 20,
        "other": 400,
    }
}
LOOP_PHASES = ["goal", "decide", "act", "observe", "evaluate", "handoff", "result", "stop"]


def last_steps(capsys) -> list[str]:
    """The step texts of the last command, from the trace log."""
    code, out = run(capsys, "trace", "--last", "1")
    return [step["text"] for step in out["records"][-1]["steps"]]


def test_advise_runs_the_loop_and_stops_on_a_checked_pick(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "advise", "--no-sync")
    assert code == 0 and out["status"] == "pick"
    assert out["next"] == {"action": "stop"}
    pick = out["pick"]
    assert pick["card_id"] == "capital-one-venture-x"
    assert pick["apply_url"].startswith("https://www.capitalone.com/")
    assert out["verifier"]["verdict"] in ("pass", "warn")
    phases = out["loop"]["phases"]
    # Every phase of the loop shows up, in order, ending in a stop.
    positions = [phases.index(phase) for phase in LOOP_PHASES]
    assert positions == sorted(positions) and phases[-1] == "stop"
    text = out["display_text"]
    assert text.startswith("🏆 My pick for you: Capital One Venture X")
    assert "🕵️ Checked by my Verifier" in text and "🔗 Apply yourself" in text
    assert markup_leaks(text) == []


def test_advise_asks_for_a_profile_before_anything_else(capsys, env):
    run(capsys, "sync", "--from-file", str(env.latest))
    code, out = run(capsys, "advise", "--no-sync")
    assert code == 0 and out["status"] == "ask_user"
    assert out["next"] == {"action": "ask_user", "reason": "no spending profile yet"}
    assert out["display_text"].startswith("🙋 To pick a card for you")
    assert out["loop"]["phases"][-1] == "ask"


def test_advise_revises_when_the_verifier_fails_a_card(capsys, env):
    setup_profile(capsys, env)
    run(capsys, "onboard", "--json", json.dumps(LOW_SPEND))
    code, out = run(capsys, "advise", "--no-sync")
    rejected = {r["card_id"]: r["reason"] for r in out["rejected"]}
    # The top two by score need $4,000 in 90 days; ~$2,661 of usual spend can't do it.
    assert list(rejected) == ["capital-one-venture-x", "capital-one-venture-rewards"]
    assert all(reason.startswith("Needs $4,000 in 90 days") for reason in rejected.values())
    assert out["status"] == "pick" and out["pick"]["card_id"] == "amex-delta-skymiles-gold"
    assert out["verifier"]["verdict"] != "fail"
    assert (
        "🔁 Skipped 2 higher-ranked cards whose minimum spend is above your usual spending: Capital One Venture X, Capital One Venture Rewards."
        in out["display_text"]
    )


def test_advise_revises_the_plan_when_minimum_spend_keeps_failing(capsys, env):
    setup_profile(capsys, env)
    tiny = {"monthly_spend": {category: 0 for category in LOW_SPEND["monthly_spend"]}}
    tiny["monthly_spend"]["other"] = 150
    run(capsys, "onboard", "--json", json.dumps(tiny))
    code, out = run(capsys, "advise", "--no-sync")
    texts = last_steps(capsys)
    # Five candidates fail on minimum spend, so the plan changes once.
    assert len(out["rejected"]) >= 5
    assert any(text.startswith("Most candidates failed on minimum spend") for text in texts)
    assert out["status"] == "pick"
    passed = {c["name"]: c["status"] for c in out["verifier"]["checks"]}
    assert passed["min_spend"] == "pass"


class AlwaysFails:
    """A stand-in Verifier that rejects every card, to reach the ask condition."""

    role = "Verifier stub"

    def __init__(self, *args):
        pass

    def run(self, brief: Brief) -> Report:
        return Report(
            brief.card_id, brief.card_id, "fail", [Check("issuer_rules", "fail", "Blocked.")]
        )


def test_advise_asks_when_nothing_passes(capsys, env):
    setup_profile(capsys, env)
    settings = Settings.from_env()
    store = Store(settings.db_path)
    advisor = Advisor(
        store,
        load_context=lambda: cli.build_context(settings, store, NOW),
        data_age=lambda: 0.0,
        refresh=None,
        now=NOW,
        make_verifier=AlwaysFails,
    )
    outcome = advisor.run(Goal())
    store.close()
    assert outcome.status == "ask_user" and outcome.reason == "no candidate passed verification"
    # Issuer-rule fails don't trigger the minimum-spend revision: five checks, then ask.
    assert len(outcome.rejected) == MAX_CHECKS
    assert outcome.question.startswith("🙋 I checked 5 cards for you and none passed:")
    assert outcome.steps[-1].phase == "ask"


def test_hidden_cards_stay_out_and_memory_says_why(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "hide", "capital one venture x", "--reason", "no Capital One for me")
    assert code == 0 and out["hidden"] == {"kind": "card", "value": "capital-one-venture-x"}
    code, out = run(capsys, "advise", "--no-sync")
    assert out["pick"]["card_id"] != "capital-one-venture-x"
    assert "🙈 Skipping 1 card or issuer you asked me to hide." in out["display_text"]
    code, out = run(capsys, "rank", "--top", "10")
    assert "capital-one-venture-x" not in {r["card_id"] for r in out["results"]}

    code, out = run(capsys, "memory")
    assert (
        '🙈 Hidden: Capital One Venture X (Sep 26, "no Capital One for me")' in out["display_text"]
    )
    assert out["memory"]["hidden"][0]["value"] == "capital-one-venture-x"

    code, out = run(capsys, "hide", "venture x", "--reason", "my card is 4111 1111 1111 1111")
    assert code == 1 and "card number" in out["error"]
    run(capsys, "hide", "--issuer", "amex")
    code, out = run(capsys, "rank", "--top", "20")
    assert not [r for r in out["results"] if r["issuer"] == "amex"]
    code, out = run(capsys, "unhide", "--all")
    assert out["cleared"] == 2
    code, out = run(capsys, "rank", "--top", "3")
    assert out["results"][0]["card_id"] == "capital-one-venture-x"


def test_advise_remembers_its_last_pick(capsys, env):
    setup_profile(capsys, env)
    run(capsys, "advise", "--no-sync")
    code, out = run(capsys, "advise", "--no-sync")
    assert "📌 Same pick as when you asked on Sep 26." in out["display_text"]
    run(capsys, "hide", "capital-one-venture-x")
    code, out = run(capsys, "advise", "--no-sync")
    assert "🔄 Changed since Sep 26, when my pick was Capital One Venture X." in out["display_text"]
    store = Store(env.tmp / "state" / "state.db")
    assert [r["card_id"] for r in store.recommendations(limit=3)][1:] == [
        "capital-one-venture-x",
        "capital-one-venture-x",
    ]
    store.close()


def test_verify_hands_over_a_bounded_brief(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "verify", "venture x")
    assert code == 0 and out["verdict"] in ("pass", "warn")
    # Only what the checks need crosses the handoff: no spend by category, no
    # valuations, no other candidates.
    assert set(out["brief"]) == {
        "card_id",
        "claimed_year1",
        "claimed_steady",
        "max_annual_fee",
        "monthly_spend",
        "credit_score_band",
        "wallet",
    }
    assert out["brief"]["monthly_spend"] == 2870
    names = [check["name"] for check in out["report"]["checks"]]
    assert names[:3] == ["available", "fee_cap", "issuer_rules"]
    assert "min_spend" in names and "official_link" in names

    code, out = run(capsys, "verify", "amex gold")  # already in the wallet
    checks = {c["name"]: c for c in out["report"]["checks"]}
    assert checks["available"] == {
        "name": "available",
        "status": "fail",
        "detail": "You already have this card.",
    }

    code, out = run(capsys, "verify", "venture x", "--max-af", "100")
    assert out["verdict"] == "fail"
    assert out["display_text"].startswith("🕵️ Verifier check: Capital One Venture X\n❌")
    assert "I wouldn't apply for this card right now." in out["display_text"]


def test_trace_replays_the_loop_and_keeps_private_values_out(capsys, env):
    setup_profile(capsys, env)
    run(capsys, "onboard", "--json", '{"monthly_spend": {"dining": 777}}')
    run(capsys, "advise", "--no-sync")
    code, out = run(capsys, "trace", "--last", "2")
    text = out["display_text"]
    assert "🕵️ Ask the Verifier to check candidate #1" in text
    assert "🛑 Stop: a checked pick is ready." in text
    advise = out["records"][-1]
    assert advise["command"] == "advise" and advise["next"] == {"action": "stop"}
    assert not [step for step in advise["steps"] if "data" in step]  # kept out of chat
    logged = [json.loads(line) for line in trace_path(env.tmp / "state").read_text().splitlines()]
    steps = [entry for entry in logged if entry["command"] == "advise"][-1]["steps"]
    handoff = next(step for step in steps if step["phase"] == "handoff")
    result = next(step for step in steps if step["phase"] == "result")
    # The handoff (role + brief) and the Verifier's report are both on record.
    assert handoff["data"]["role"].startswith("Verifier:")
    assert result["data"]["report"]["card_id"] == handoff["data"]["brief"]["card_id"]
    raw = trace_path(env.tmp / "state").read_text()
    assert "777" not in raw  # onboard JSON is never logged
    assert out["records"][0]["argv"] == ["onboard", "--json", "…"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["onboard", "--json", '{"x": 1}'], ["onboard", "--json", "…"]),
        (["onboard", "--json={}"], ["onboard", "--json=…"]),
        (["hide", "gold", "--reason", "nope"], ["hide", "gold", "--reason", "…"]),
        (["rank", "--top", "3"], ["rank", "--top", "3"]),
    ],
)
def test_safe_argv(argv, expected):
    assert safe_argv(argv) == expected


def test_typos_are_read_on_reads_and_questioned_on_writes(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "explain", "saphire", "reserve")
    assert code == 0
    assert out["display_text"].startswith("🔤 I read 'saphire reserve' as Chase Sapphire Reserve.")
    assert out["evaluation"]["card_id"] == "chase-sapphire-reserve"
    # Writing to memory never acts on a guess.
    code, out = run(capsys, "wallet", "add", "saphire", "reserve")
    assert code == 1 and out["next"] == {"action": "ask_user", "reason": "unknown card name"}
    assert "Did you mean Chase Sapphire Reserve" in out["error"]


def test_every_command_speaks_plain_text(capsys, env):
    setup_profile(capsys, env)
    run(
        capsys,
        "onboard",
        "--json",
        '{"profile": {"credit_score_band": "good", "total_credit_limit": 20000}}',
    )
    commands = [
        ["advise", "--no-sync"],
        ["rank", "--top", "3"],
        ["explain", "amex platinum"],
        ["compare", "venture x", "chase-sapphire-reserve"],
        ["verify", "venture x"],
        ["apply-link", "venture x"],
        ["credit"],
        ["use"],
        ["use", "uber"],
        ["memory"],
        ["onboard", "--show"],
        ["wallet", "list"],
        ["trace"],
        ["digest", "--no-sync"],
    ]
    for argv in commands:
        code, out = run(capsys, *argv)
        assert code == 0, (argv, out)
        assert markup_leaks(out["display_text"]) == [], argv
        assert out["next"]["action"] in ("stop", "ask_user")


def test_use_apply_link_and_credit(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "use", "groceries")
    row = out["use"][0]
    assert row["category"] == "groceries" and row["best"]["card_id"] == "amex-gold"
    assert out["display_text"].splitlines()[2].startswith("🛒 Groceries: Amex Gold (4x")
    code, out = run(capsys, "use", "bowling")
    assert code == 1 and out["next"]["reason"] == "unknown category"

    code, out = run(capsys, "apply-link", "venture x")
    assert out["apply_url"] == "https://www.capitalone.com/credit-cards/venture-x/"
    assert out["prequalify_url"] == "https://www.capitalone.com/apply/credit-cards/preapprove/"
    assert "I never apply for you" in out["display_text"]

    run(
        capsys,
        "onboard",
        "--json",
        '{"profile": {"credit_score_band": "fair", "total_credit_limit": 8000}}',
    )
    code, out = run(capsys, "credit")
    credit = out["credit"]
    assert credit["chase_524_count"] == 1
    assert credit["utilization"] == pytest.approx(2870 / 8000, abs=0.001)
    text = out["display_text"]
    assert "⚠️ If your statements show about a month of spending, that's ~36%" in text
    assert "🎯 Score range fair (580-669)" in text
    assert "35% Payment history" in text and "https://www.annualcreditreport.com/" in text


def test_trace_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "MAX_BYTES", 2_000)
    monkeypatch.setattr(trace, "KEEP_LINES", 5)
    for i in range(40):
        payload = {"command": "rank", "ok": True, "display_text": f"line {i}"}
        trace.record(tmp_path, NOW, ["rank"], payload, 1.0)
    lines = trace_path(tmp_path).read_text().splitlines()
    assert len(lines) <= 20 and json.loads(lines[-1])["summary"] == "line 39"
    assert [r["summary"] for r in trace.read(tmp_path, last=2)] == ["line 38", "line 39"]
