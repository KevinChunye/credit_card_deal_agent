"""The Advisor: the main agent loop behind `advise` ("which card should I get?").

    goal -> decide -> act (a tool) -> observe -> evaluate -> continue, stop or ask you

1. Memory: read your saved spending, wallet and hidden cards. No spending on
   file -> stop and ask you for it (ask-a-person condition 1).
2. Data: card data older than REFRESH_AFTER_DAYS (or missing) is refreshed.
   The refresh retries and switches endpoints by itself; if it still fails,
   the loop carries on with the saved copy and says so. No data at all ->
   stop and ask (condition 2).
3. Rank every card for you with the deterministic scorer (hidden cards and
   issuers skipped). Nothing fits your filters -> stop and ask (condition 3).
4. Delegate: hand the best candidate to the Verifier subagent (verifier.py)
   with a bounded brief and read its report. A fail rejects the card and the
   next one is handed over; pass or warn accepts it, caveats attached. If all
   MAX_CHECKS candidates fail and minimum spend is the usual reason, revise
   the plan once: re-filter to cards whose minimum spend fits your spending
   and check up to MAX_CHECKS of those.
5. Stop when a card is accepted, and save the pick to memory. Stop and ask
   you if nothing passes (condition 4). MAX_STEPS is a safety net against a
   runaway loop.

Every step is recorded; `advise` returns them and the CLI writes them to the
trace log, which `trace` replays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from card_agent.digest import money
from card_agent.present import plural
from card_agent.scoring import CardEvaluation, ScoringContext, rank
from card_agent.snapshot import Refresh
from card_agent.store import Store
from card_agent.verifier import Brief, Report, Verifier

MAX_CHECKS = 5
MAX_STEPS = 60
REFRESH_AFTER_DAYS = 7

MODE_LABEL = {
    "travel": "travel cards",
    "cash_back": "cash back cards",
    "business": "business cards",
}

ONBOARDING_QUESTION = "\n".join(
    [
        "🙋 To pick a card for you I need a few details first:",
        "1️⃣ Your main goal: travel, cash back, business, building credit, or sign-up bonuses?",
        "2️⃣ The most you'd pay in annual fees",
        "3️⃣ Your rough monthly spending on dining, groceries, gas, travel and everything else",
        "4️⃣ Cards you already have, and about when you opened them",
        "5️⃣ How many trips you take a year",
        "Your answers stay private in your own database.",
    ]
)


class LoopLimit(RuntimeError):
    """The loop hit MAX_STEPS; a bug guard, not an expected outcome."""


@dataclass(frozen=True)
class Goal:
    mode: str | None = None
    kind: str | None = None
    max_af: float | None = None

    @property
    def key(self) -> str:
        cap = "profile" if self.max_af is None else f"{self.max_af:g}"
        return f"mode={self.mode or 'any'};kind={self.kind or 'default'};max_af={cap}"

    def describe(self, profile_cap: float) -> str:
        cap = profile_cap if self.max_af is None else self.max_af
        scope = f" among {MODE_LABEL[self.mode]}" if self.mode else ""
        return (
            f"Find the best card for you to apply for next{scope}, annual fee up to {money(cap)}."
        )


@dataclass
class Step:
    n: int
    phase: str  # goal | decide | act | observe | evaluate | handoff | result | stop | ask
    text: str
    data: dict = field(default_factory=dict)

    def to_dict(self, with_data: bool = True) -> dict:
        base = {"n": self.n, "phase": self.phase, "text": self.text}
        return base | ({"data": self.data} if with_data and self.data else {})


@dataclass
class Outcome:
    status: str  # pick | ask_user
    goal: Goal
    steps: list[Step]
    reason: str
    pick: CardEvaluation | None = None
    report: Report | None = None
    rejected: list[Report] = field(default_factory=list)
    runners_up: list[CardEvaluation] = field(default_factory=list)
    question: str | None = None
    notes: list[str] = field(default_factory=list)
    previous: dict | None = None  # the last pick for the same goal, from memory
    verifier_calls: int = 0


class Advisor:
    def __init__(
        self,
        store: Store,
        load_context: Callable[[], ScoringContext],
        data_age: Callable[[], float | None],
        refresh: Callable[[], Refresh] | None,
        now: datetime,
        max_checks: int = MAX_CHECKS,
        make_verifier: Callable[..., Verifier] = Verifier,
    ):
        self.store = store
        self.make_verifier = make_verifier
        self.load_context = load_context
        self.data_age = data_age
        self.refresh = refresh
        self.now = now
        self.max_checks = max_checks
        self.steps: list[Step] = []
        self.notes: list[str] = []

    def log(self, phase: str, text: str, **data) -> None:
        if len(self.steps) >= MAX_STEPS:
            raise LoopLimit(f"stopped after {MAX_STEPS} steps")
        self.steps.append(Step(len(self.steps) + 1, phase, text, data))

    def ask(self, goal: Goal, reason: str, question: str, **extra) -> Outcome:
        self.log("ask", f"Stop and ask you: {reason}.")
        return Outcome(
            "ask_user", goal, self.steps, reason, question=question, notes=self.notes, **extra
        )

    # ------------------------------------------------------------------ loop
    def run(self, goal: Goal) -> Outcome:
        profile = self.store.get_profile()
        self.log("goal", goal.describe(profile.max_annual_fee))

        # 1. Memory.
        self.log("decide", "Check what I remember about you first.")
        spend = self.store.get_spend()
        wallet = [w for w in self.store.list_wallet() if w.is_open]
        hidden = self.store.hidden()
        self.log("act", "Read your spending, wallet and hidden cards from memory.", tool="memory")
        self.log(
            "observe",
            f"{money(sum(spend.values()))}/mo of spending on file, "
            f"{plural(len(wallet), 'open card')}, {len(hidden)} hidden.",
        )
        if not spend:
            return self.ask(goal, "no spending profile yet", ONBOARDING_QUESTION)
        self.log("evaluate", "Enough to rank cards, so continue.")

        # 2. Data.
        age = self.data_age()
        if age is None or (age > REFRESH_AFTER_DAYS and self.refresh):
            why = (
                "No saved card data yet"
                if age is None
                else f"Card data is {plural(round(age), 'day')} old"
            )
            if self.refresh is None:
                return self.ask(
                    goal,
                    "no card data",
                    "🙋 I don't have any card data yet. Shall I download it now?",
                )
            self.log("decide", f"{why}, so refresh it.")
            result = self.refresh()
            self.log(
                "act",
                "Download the latest card data (retries and a second endpoint built in).",
                tool="sync",
                attempts=result.attempts,
            )
            if result.ok:
                self.log("observe", f"Fresh data: {result.summary['cards']} cards.")
            elif result.usable:
                self.log(
                    "observe",
                    f"Refresh failed after {plural(len(result.attempts), 'try', 'tries')} "
                    f"({result.plain_reason}); a saved copy from "
                    f"{plural(round(result.saved_age_days), 'day')} ago is still here.",
                )
                self.notes.append(
                    "⚠️ I couldn't refresh the card data just now, so this uses the saved copy "
                    f"from {plural(round(result.saved_age_days), 'day')} ago."
                )
            else:
                return self.ask(
                    goal,
                    "card data unavailable",
                    "🙋 I can't reach the card data right now and have no saved copy. "
                    "Try again in a few minutes?",
                )
            self.log("evaluate", "Card data is usable, so continue.")
            age = self.data_age()
        else:
            self.log("decide", "Check how fresh the saved card data is.")
            self.log("observe", f"Card data is {plural(round(age), 'day')} old: fresh enough.")

        # 3. Rank.
        ctx = self.load_context()
        self.log("decide", "Rank every card against your wallet and spending.")
        frame, evaluations = rank(ctx, mode=goal.mode, kind=goal.kind, max_annual_fee=goal.max_af)
        self.log("act", "Score all cards with the deterministic scorer.", tool="scorer")
        self.log("observe", f"{plural(len(frame), 'card')} fit your filters.")
        if hidden:
            self.notes.append(
                f"🙈 Skipping {plural(len(hidden), 'card or issuer', 'cards or issuers')} "
                "you asked me to hide."
            )
        if frame.empty:
            return self.ask(
                goal,
                "no card fits the filters",
                "🙋 No card fits these filters right now. Want me to raise the annual fee "
                "limit, include business cards, or show hidden cards?",
            )
        self.log("evaluate", "Before recommending the top card, get it checked.")

        # 4. Delegate to the Verifier; revise on fails.
        verifier = self.make_verifier(ctx.data, ctx.rules, ctx.today, age)
        rejected: list[Report] = []
        calls = 0
        queue = list(frame["card_id"].head(self.max_checks))
        revised = False
        while queue:
            ev = evaluations[queue.pop(0)]
            brief = Brief.for_candidate(ev, ctx, goal.max_af)
            self.log(
                "handoff",
                f"Ask the Verifier to check candidate #{calls + 1}, {ev.name}.",
                tool="verifier",
                role=verifier.role,
                brief=brief.to_dict(),
            )
            report = verifier.run(brief)
            calls += 1
            self.log(
                "result",
                f"Verifier on {ev.name}: {report.verdict} ({report.summary}).",
                report=report.to_dict(),
            )
            if report.verdict != "fail":
                caveats = len(report.with_status("warn"))
                note = f", with {plural(caveats, 'caveat')}" if caveats else ""
                self.log("evaluate", f"Accept {ev.name}{note}.")
                return self._finish(goal, ev, report, rejected, frame, evaluations, calls)
            self.log(
                "evaluate",
                f"Reject it ({report.with_status('fail')[0].detail}), try the next card.",
            )
            rejected.append(report)
            if queue or revised:
                continue
            # Out of candidates. If minimum spend is what keeps failing, change the
            # plan once: only cards whose minimum spend fits your usual spending.
            reasons = pd.Series([r.with_status("fail")[0].name for r in rejected])
            if (reasons == "min_spend").mean() < 0.5:
                break
            revised = True
            done = {r.card_id for r in rejected}
            fits = frame[frame["hits_min_spend"].ne(False) & ~frame["card_id"].isin(done)]
            self.log(
                "decide",
                "Most candidates failed on minimum spend, so revise the plan: consider only "
                "cards whose minimum spend fits your usual spending.",
            )
            self.log("act", "Re-filter the ranking by reachable minimum spend.", tool="scorer")
            queue = list(fits["card_id"].head(self.max_checks))
            self.log("observe", f"{plural(len(fits), 'card')} fit; checking the top {len(queue)}.")

        self.log("evaluate", f"All {plural(len(rejected), 'candidate')} failed verification.")
        lines = [f"🙋 I checked {plural(len(rejected), 'card')} for you and none passed:"]
        lines += [f"❌ {r.card_name}: {r.with_status('fail')[0].detail}" for r in rejected[:5]]
        if len(rejected) > 5:
            lines.append(f"…and {len(rejected) - 5} more.")
        lines.append(
            "Has your spending changed, or should I raise the annual fee limit or include "
            "business cards?"
            if revised
            else "Should I look at cards with smaller minimum spends (for example no-annual-fee "
            "cards), or has your spending changed?"
        )
        return self.ask(
            goal, "no candidate passed verification", "\n".join(lines), rejected=rejected
        )

    def _finish(
        self,
        goal: Goal,
        ev: CardEvaluation,
        report: Report,
        rejected: list[Report],
        frame,
        evaluations: dict[str, CardEvaluation],
        calls: int,
    ) -> Outcome:
        skip = {ev.card_id} | {r.card_id for r in rejected}
        runners = [evaluations[cid] for cid in frame["card_id"] if cid not in skip][:3]
        previous = self.store.recommendations(limit=1, goal=goal.key)
        self.log("act", "Save this pick to memory for next time.", tool="memory")
        self.store.log_recommendation(
            self.now,
            goal.key,
            ev.card_id,
            report.verdict,
            {
                "name": ev.name,
                "year1": round(ev.marginal_ev_year1, 2),
                "steady": round(ev.marginal_ev_steady, 2),
                "rejected": [r.card_id for r in rejected],
            },
        )
        self.log("stop", "Stop: a checked pick is ready.")
        return Outcome(
            "pick",
            goal,
            self.steps,
            "a checked pick is ready",
            pick=ev,
            report=report,
            rejected=rejected,
            runners_up=runners,
            notes=self.notes,
            previous=previous[0] if previous else None,
            verifier_calls=calls,
        )
