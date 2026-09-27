"""Baseline vs improved: five scripted conversations, measured.

Each scenario is something a person says in chat, played by the policy that
each SKILL.md version tells the agent to follow:

    baseline  the code and SKILL.md at BASELINE_REF (before the agent loop,
              the Verifier, memory, recovery and the chat formatting)
    improved  this checkout

The CLI runs as a subprocess from each checkout, against the same pinned
card data and a fresh state DB per scenario, so both face exactly the same
question. No LLM is called and nothing depends on the network: the outage
scenario points HTTPS_PROXY at a dead local port.

Metrics per scenario and configuration:
    success        the scenario's own check (see each scenario's docstring)
    tool calls     CLI commands the agent runs for the request
    interventions  times the agent must go back to the person before the goal
                   is met (the first request doesn't count)
    latency        wall time of those tool calls
    tokens         what the chat model has to read: tool stdout chars / 4
    quality        text shown to the person that leaks markup, command names
                   or exception names (0 is best)
    cost           tool cost; always $0.00 (no paid API calls in the tools)

Usage:
    python scripts/eval_agent.py [--snapshot latest.json] [--baseline-ref REF]
                                 [--json results.json]
Without --snapshot it reads data/latest.json from origin/data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from card_agent.present import markup_leaks  # noqa: E402

BASELINE_REF = "a568dd7"  # main before this change
# Command names, flags and exception names a person should never have to read.
JARGON = re.compile(r"\b(onboard|sync|rank|card_id)\b|--[a-z]|\b\w+Error\b|Errno|Traceback", re.I)
# A person who spends about $1,000 a month: big bonuses with $4,000 minimums
# are out of reach for them, even over 120 days.
STARTER = {
    "profile": {"max_annual_fee": 400, "trips_per_year": 2},
    "monthly_spend": {
        "dining": 200,
        "groceries": 350,
        "gas": 100,
        "flights": 40,
        "hotels": 40,
        "transit_rideshare": 20,
        "streaming": 30,
        "drugstores": 20,
        "other": 200,
    },
}
# A dead proxy: every download fails the way it does when the network is down.
# Both spellings: lowercase proxy variables win when both are set.
DEAD_PROXY = "http://127.0.0.1:9"
NETWORK_DOWN = {
    name: DEAD_PROXY
    for base in ("https_proxy", "http_proxy", "all_proxy")
    for name in (base, base.upper())
} | {"no_proxy": "", "NO_PROXY": ""}


@dataclass
class Call:
    argv: list[str]
    ok: bool
    ms: float
    chars: int


@dataclass
class Result:
    scenario: str
    config: str
    success: bool
    note: str
    calls: list[Call] = field(default_factory=list)
    interventions: int = 0
    quality_issues: list[str] = field(default_factory=list)

    @property
    def latency_ms(self) -> float:
        return sum(call.ms for call in self.calls)

    @property
    def tokens(self) -> int:
        return round(sum(call.chars for call in self.calls) / 4)


class Agent:
    """Runs one configuration's CLI for one scenario and keeps score."""

    def __init__(self, config: str, checkout: Path, state: Path, snapshot: Path):
        self.config = config
        self.checkout = checkout
        self.snapshot = snapshot
        self.env = {
            **os.environ,
            "CARD_AGENT_DB": str(state / "state.db"),
            "PYTHONPATH": str(checkout),
        }
        for var in ("AGENTMAIL_API_KEY", "AGENTMAIL_INBOX", "OWNER_EMAIL", "GITHUB_TOKEN"):
            self.env.pop(var, None)
        self.calls: list[Call] = []
        self.interventions = 0
        self.issues: list[str] = []

    @property
    def baseline(self) -> bool:
        return self.config == "baseline"

    def _exec(
        self, argv: list[str], extra_env: dict[str, str] | None = None
    ) -> tuple[dict, float, int]:
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "card_agent", *argv],
            cwd=self.checkout,
            env={**self.env, **(extra_env or {})},
            capture_output=True,
            text=True,
            timeout=180,
        )
        ms = (time.perf_counter() - started) * 1000
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {"ok": False, "error": (proc.stdout + proc.stderr).strip()[-500:]}
        return out, ms, len(proc.stdout)

    def setup(self, *argv: str) -> dict:
        """Earlier sessions (loading data, the person's profile): not scored."""
        out, _, _ = self._exec(list(argv))
        if not out.get("ok"):
            raise RuntimeError(f"{self.config} setup {argv} failed: {out}")
        return out

    def tool(self, *argv: str, extra_env: dict[str, str] | None = None) -> dict:
        out, ms, chars = self._exec(list(argv), extra_env)
        self.calls.append(Call(list(argv), bool(out.get("ok")), round(ms), chars))
        return out

    def show(self, out: dict) -> str:
        """What the person reads: display_text, or the error (the old SKILL.md
        says to tell the person the error plainly)."""
        text = out.get("display_text") or out.get("error") or ""
        self.issues += [f"markup: {leak}" for leak in markup_leaks(text)]
        self.issues += [f"jargon: {m.group(0)}" for m in JARGON.finditer(text)]
        return text

    def result(self, scenario: str, success: bool, note: str) -> Result:
        return Result(
            scenario, self.config, success, note, self.calls, self.interventions, self.issues
        )


# ---------------------------------------------------------------------------
# Scenarios. Each gets a fresh Agent and returns (success, note).
# ---------------------------------------------------------------------------


def s1_new_user(agent: Agent, judge: Callable) -> tuple[bool, str]:
    """ "What card should I get?" from someone the agent knows nothing about.
    Success: the agent asks for the missing details in plain words (no
    command names or error jargon), so the person knows what to answer."""
    agent.setup("sync", "--from-file", str(agent.snapshot))
    out = agent.tool("rank") if agent.baseline else agent.tool("advise", "--no-sync")
    text = agent.show(out)
    agent.interventions += 1  # it must ask; that's the right move for both
    plain = not JARGON.search(text) and not markup_leaks(text)
    asks = "spend" in text.lower()
    return asks and plain, text.splitlines()[0][:90]


def s2_recommend(agent: Agent, judge: Callable) -> tuple[bool, str]:
    """A returning user who spends about $1,000 a month asks "what card should
    I get?". Success: the card the agent recommends first survives the
    Verifier's hard checks (run as an independent judge on the same profile):
    minimum spend reachable, eligible, within the fee limit, offer not expired."""
    agent.setup("sync", "--from-file", str(agent.snapshot))
    agent.setup("onboard", "--from-yaml", str(REPO / "config" / "user_profile.example.yaml"))
    agent.setup("onboard", "--json", json.dumps(STARTER))
    if agent.baseline:
        out = agent.tool("rank")
        pick = out["results"][0]["card_id"] if out.get("results") else None
    else:
        out = agent.tool("advise", "--no-sync")
        pick = (out.get("pick") or {}).get("card_id")
    agent.show(out)
    if pick is None:
        return False, "no recommendation"
    verdict, failed = judge(pick)
    note = f"recommends {pick}: judge says {verdict}" + (f" ({failed})" if failed else "")
    return verdict != "fail", note


def s3_typo(agent: Agent, judge: Callable) -> tuple[bool, str]:
    """ "Explain saphire prefered" (a typo). Success: the right card is
    explained without asking the person to retype it."""
    agent.setup("sync", "--from-file", str(agent.snapshot))
    agent.setup("onboard", "--from-yaml", str(REPO / "config" / "user_profile.example.yaml"))
    out = agent.tool("explain", "saphire prefered")
    agent.show(out)
    if not out.get("ok"):
        agent.interventions += 1  # "which card do you mean?"
        out = agent.tool("explain", "chase sapphire preferred")  # the person retypes it
        agent.show(out)
    card = (out.get("evaluation") or {}).get("card_id")
    right = card == "chase-sapphire-preferred"
    return right and agent.interventions == 0, f"explained {card} after {len(agent.calls)} call(s)"


def s4_outage(agent: Agent, judge: Callable) -> tuple[bool, str]:
    """ "Refresh my card data" while the network is down (a saved copy from an
    earlier sync exists). Success: the agent detects the failure, recovers
    (retry, other endpoint, saved copy) and tells the person the saved data
    still works, instead of handing them an error."""
    agent.setup("sync", "--from-file", str(agent.snapshot))
    out = agent.tool("sync", extra_env=NETWORK_DOWN)
    text = agent.show(out)
    if not out.get("ok"):
        agent.interventions += 1  # the person is left to retry later
    recovered = bool(out.get("ok")) and (out.get("sync") or {}).get("status") == "saved_copy"
    return recovered, text.splitlines()[0][:90]


def s5_memory(agent: Agent, judge: Callable) -> tuple[bool, str]:
    """Session 1: "what card should I get?", then "I'll never get that one,
    stop suggesting it". Session 2 (a new chat): "what card should I get?".
    Success: the rejected card is not recommended again."""
    agent.setup("sync", "--from-file", str(agent.snapshot))
    agent.setup("onboard", "--from-yaml", str(REPO / "config" / "user_profile.example.yaml"))
    if agent.baseline:
        first = agent.tool("rank")
        top = first["results"][0]["card_id"]
        # No command stores a preference: the old agent can only say "noted"
        # in a chat that the next session doesn't see.
        second = agent.tool("rank")
        again = [r["card_id"] for r in second["results"]]
    else:
        first = agent.tool("advise", "--no-sync")
        top = first["pick"]["card_id"]
        agent.show(agent.tool("hide", top, "--reason", "not interested"))
        second = agent.tool("advise", "--no-sync")
        again = [(second.get("pick") or {}).get("card_id")]
    agent.show(second)
    if top in again:
        agent.interventions += 1  # the person has to say it all over again
    return top not in again, f"hid {top}; next session shows it: {top in again}"


def c_typical(agent: Agent, judge: Callable) -> tuple[bool, str]:
    """Control, not scored: the happy path the baseline already handled. The
    example profile ($2,870/mo) asks "what card should I get?". Success: the
    first recommendation survives the judge on that profile. It shows the new
    loop adds no calls and no regression when nothing goes wrong."""
    agent.setup("sync", "--from-file", str(agent.snapshot))
    agent.setup("onboard", "--from-yaml", str(REPO / "config" / "user_profile.example.yaml"))
    if agent.baseline:
        out = agent.tool("rank")
        pick = out["results"][0]["card_id"]
    else:
        out = agent.tool("advise", "--no-sync")
        pick = out["pick"]["card_id"]
    agent.show(out)
    verdict, _ = judge(pick, typical=True)
    return verdict != "fail", f"recommends {pick}: judge says {verdict}"


SCENARIOS: list[tuple[str, Callable]] = [
    ("S1 New user asks for a card", s1_new_user),
    ("S2 Recommendation for a $1,000/mo spender", s2_recommend),
    ("S3 Typo in a card name", s3_typo),
    ("S4 Card data server down", s4_outage),
    ("S5 Remembers a rejected card", s5_memory),
]
CONTROL = ("C  Typical spender asks for a card (control)", c_typical)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_judge(workdir: Path, snapshot: Path) -> Callable[..., tuple[str, str]]:
    """The improved Verifier as an independent grader, on the scenario's profile."""
    judges = {}
    for name, extra in (("starter", STARTER), ("typical", None)):
        judge = Agent("judge", REPO, workdir / f"judge-{name}", snapshot)
        judge.setup("sync", "--from-file", str(snapshot))
        judge.setup("onboard", "--from-yaml", str(REPO / "config" / "user_profile.example.yaml"))
        if extra:
            judge.setup("onboard", "--json", json.dumps(extra))
        judges[name] = judge

    def verdict(card_id: str, typical: bool = False) -> tuple[str, str]:
        out, _, _ = judges["typical" if typical else "starter"]._exec(["verify", card_id])
        failed = [c["detail"] for c in out["report"]["checks"] if c["status"] == "fail"]
        return out["verdict"], "; ".join(failed)

    return verdict


def run_all(baseline: Path, snapshot: Path, workdir: Path) -> list[Result]:
    judge = make_judge(workdir, snapshot)
    results = []
    for name, scenario in [*SCENARIOS, CONTROL]:
        for config, checkout in (("baseline", baseline), ("improved", REPO)):
            state = workdir / config / name.split()[0]
            agent = Agent(config, checkout, state, snapshot)
            success, note = scenario(agent, judge)
            results.append(agent.result(name, success, note))
            print(f"{name:45} {config:9} {'✅' if success else '❌'} {note}", file=sys.stderr)
    return results


def table(results: list[Result]) -> str:
    lines = [
        "| Scenario | Config | Success | Tool calls | Human interventions | Latency (ms) | Tokens read | Quality issues |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        issues = ", ".join(sorted(set(r.quality_issues))) or "none"
        lines.append(
            f"| {r.scenario} | {r.config} | {'✅' if r.success else '❌'} | {len(r.calls)} | "
            f"{r.interventions} | {r.latency_ms:,.0f} | {r.tokens:,} | {issues} |"
        )
    lines += [
        "",
        f"Summary over S1-S{len(SCENARIOS)} (the control row is not scored):",
        "",
        "| Config | Success rate | Tool calls | Human interventions | Median latency per call (ms) | Tokens read | Quality issues | Tool cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for config in ("baseline", "improved"):
        rows = [r for r in results if r.config == config and r.scenario != CONTROL[0]]
        per_call = [c.ms for r in rows for c in r.calls]
        lines.append(
            f"| {config} | {sum(r.success for r in rows)}/{len(rows)} | "
            f"{sum(len(r.calls) for r in rows)} | {sum(r.interventions for r in rows)} | "
            f"{statistics.median(per_call):,.0f} | {sum(r.tokens for r in rows):,} | "
            f"{sum(len(r.quality_issues) for r in rows)} | $0.00 |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--snapshot", type=Path, help="a data/latest.json to pin (default: origin/data)"
    )
    parser.add_argument("--baseline-ref", default=BASELINE_REF)
    parser.add_argument("--json", type=Path, help="also write the raw results here")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="card-eval-") as tmp:
        workdir = Path(tmp)
        snapshot = args.snapshot
        if snapshot is None:
            snapshot = workdir / "data" / "latest.json"
            snapshot.parent.mkdir(parents=True)
            blob = subprocess.run(
                ["git", "show", "origin/data:data/latest.json"],
                cwd=REPO,
                capture_output=True,
                check=True,
            ).stdout
            snapshot.write_bytes(blob)
        baseline = workdir / "baseline"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(baseline), args.baseline_ref],
            cwd=REPO, check=True, capture_output=True,
        )  # fmt: skip
        try:
            results = run_all(baseline, snapshot.resolve(), workdir)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(baseline)], cwd=REPO, check=False
            )
        generated = json.loads(snapshot.read_text())["generated_at"]
    print(f"Card data: snapshot generated {generated}; baseline {args.baseline_ref}.\n")
    print(table(results))
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
