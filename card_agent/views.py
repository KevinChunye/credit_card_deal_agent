"""Display text for every command.

Plain words, emoji and bar charts, ready to paste into a chat or read in an
email (see present.py for the rules). All numbers come from the scorer,
verifier or store; this module only lays them out.
"""

from __future__ import annotations

from datetime import date, datetime

from card_agent.advisor import Outcome
from card_agent.credit import FICO_FACTORS, HABITS, CreditHealth
from card_agent.digest import money
from card_agent.freshness import MARKER, terms_warning
from card_agent.links import host
from card_agent.models import ISSUER_DISPLAY, Card, Category, UserProfile, WalletCard
from card_agent.present import (
    bar,
    bar_lines,
    category_emoji,
    category_label,
    currency_label,
    number,
    plural,
)
from card_agent.scoring import CardEvaluation
from card_agent.verifier import Report

ELIGIBILITY_ICON = {"eligible": "✅", "unknown": "❓", "ineligible": "🚫"}
GOAL_LABEL = {
    "travel": "travel",
    "cash_back": "cash back",
    "business": "business",
    "credit_building": "building credit",
    "bonus_churning": "sign-up bonuses",
}
PHASE_ICON = {
    "goal": "🎯",
    "decide": "🤔",
    "act": "🛠️",
    "observe": "👀",
    "evaluate": "⚖️",
    "handoff": "🕵️",
    "result": "📋",
    "stop": "🛑",
    "ask": "🙋",
}


def signed(value: float) -> str:
    return money(value, True)


def _category_of(label: str) -> Category | None:
    """ "Earn: dining" -> Category.dining."""
    if not label.startswith("Earn: "):
        return None
    try:
        return Category(label.removeprefix("Earn: "))
    except ValueError:
        return None


def link_lines(apply_url: str | None, prequal_url: str | None, issuer_name: str) -> list[str]:
    lines = []
    if apply_url:
        lines.append(f"🔗 Apply yourself on {issuer_name}'s own page: {apply_url}")
    else:
        lines.append(
            f"🔗 Apply only on {issuer_name}'s own website; I don't have a link I can vouch for."
        )
    if prequal_url:
        lines.append(
            f"🔍 Check for pre-approval first (soft pull, no effect on your score): {prequal_url}"
        )
    return lines


def issuer_name(card: Card) -> str:
    return card.display_name.removesuffix(card.name).strip() or card.issuer.title()


# ---------------------------------------------------------------------- rank
def rank_flags(ev: CardEvaluation, card: Card, today: date) -> list[str]:
    flags = []
    if ev.eligibility != "eligible":
        reason = f": {ev.eligibility_reasons[0]}" if ev.eligibility_reasons else ""
        word = "not eligible" if ev.eligibility == "ineligible" else "eligibility unknown"
        flags.append(f"{ELIGIBILITY_ICON[ev.eligibility]} {word}{reason}")
    if ev.hits_min_spend is False:
        flags.append("🧮 minimum spend is above your usual spending")
    warning = terms_warning(card, today)
    if warning:
        flags.append(f"{MARKER} {warning}")
    return flags


def rank_text(
    evaluations: list[CardEvaluation],
    cards: dict[str, Card],
    scope: str,
    today: date,
    notes: list[str],
) -> str:
    lines = [
        f"🏆 Your top {plural(len(evaluations), 'card')} ({scope}), valued against the cards "
        "you already have",
        "",
    ]
    top = max((ev.marginal_ev_year1 for ev in evaluations), default=0.0)
    stale = False
    for i, ev in enumerate(evaluations, 1):
        lines.append(f"{number(i)} {ev.name} · {money(ev.annual_fee)} fee")
        lines.append(
            f"{bar(ev.marginal_ev_year1, top)} {signed(ev.marginal_ev_year1)} year 1 · "
            f"{signed(ev.marginal_ev_steady)}/yr after"
        )
        if ev.offer_summary:
            lines.append(f"🎁 {ev.offer_summary}")
        flags = rank_flags(ev, cards[ev.card_id], today)
        stale = stale or any(flag.startswith(MARKER) for flag in flags)
        lines += flags
        lines.append("")
    if stale:
        lines.append(
            f"{MARKER} = terms not verified against the issuer's page in the last 60 days; "
            "check the fee and rewards before relying on them."
        )
    lines += notes
    if evaluations:
        first = evaluations[0].name
        lines.append(
            f'💬 Want the math? Say "explain {first}". Thinking of applying? Ask '
            f'"should I get {first}?" and I\'ll run my checks.'
        )
    return "\n".join(lines).strip()


# ------------------------------------------------------------------- explain
def _terms_line(card: Card, today: date) -> str:
    warning = terms_warning(card, today)
    if warning:
        return f"{MARKER} {warning[0].upper()}{warning[1:]}."
    return f"✔️ Terms verified {card.last_verified:%Y-%m-%d} on {host(card.source_url or '')}."


def explain_text(ev: CardEvaluation, card: Card, today: date) -> str:
    lines = [
        f"🔎 {ev.name}",
        f"On its own: {signed(ev.year1_ev)} in year 1, then {signed(ev.steady_ev)}/yr.",
        "",
    ]
    earn = [line for line in ev.breakdown if _category_of(line.label)]
    benefits = [line for line in ev.breakdown if line.label.startswith("Benefit: ")]
    for line in ev.breakdown:
        if line.label == "Sign-up bonus":
            lines.append(f"🎁 Sign-up bonus: {signed(line.amount)} ({line.detail})")
    if earn:
        lines.append(f"💰 Rewards on your spending: {signed(ev.earn_value)}/yr")
        for line in earn:
            category = _category_of(line.label)
            lines.append(
                f"   {category_emoji(category)} {category_label(category)}: "
                f"{signed(line.amount)} ({line.detail})"
            )
    if benefits:
        lines.append(f"🎟️ Credits and perks: {signed(ev.benefits_value_year1)} in year 1")
        for line in benefits:
            lines.append(
                f"   • {line.label.removeprefix('Benefit: ')}: {signed(line.amount)} ({line.detail})"
            )
    for line in ev.breakdown:
        if line.label.startswith("Annual fee"):
            detail = f" ({line.detail})" if line.detail else ""
            lines.append(f"💵 Annual fee in year 1: {money(line.amount)}{detail}")
    lines += [
        "",
        f"➕ Versus the cards you have: {signed(ev.marginal_ev_year1)} in year 1, then "
        f"{signed(ev.marginal_ev_steady)}/yr",
    ]
    for line in ev.marginal_breakdown:
        category = _category_of(line.label)
        if category and line.amount:
            lines.append(
                f"   {category_emoji(category)} {category_label(category)}: "
                f"{signed(line.amount)} ({line.detail})"
            )
        elif line.label.startswith("Benefit: ") and (
            line.amount or line.detail.startswith("already")
        ):
            name = line.label.removeprefix("Benefit: ")
            lines.append(f"   🎟️ {name}: {signed(line.amount)} ({line.detail})")
    lines.append(f"📏 Versus a flat 2% card: {signed(ev.vs_flat_2pct_steady)}/yr after year 1")
    icon = ELIGIBILITY_ICON[ev.eligibility]
    lines.append(f"{icon} Eligibility: {ev.eligibility}")
    lines += [f"   {reason}" for reason in ev.eligibility_reasons]
    lines += [f"📝 {note[0].upper()}{note[1:]}." for note in ev.notes]
    lines.append(_terms_line(card, today))
    return "\n".join(lines)


# ------------------------------------------------------------------- compare
def compare_text(
    a: CardEvaluation, b: CardEvaluation, card_a: Card, card_b: Card, today: date
) -> str:
    def verified(card: Card) -> str:
        shown = f"{card.last_verified:%Y-%m-%d}" if card.last_verified else "never"
        return shown if terms_warning(card, today) is None else f"{MARKER} {shown}"

    def spend_ok(ev: CardEvaluation) -> str:
        return {True: "yes", False: "no", None: "no bonus"}[ev.hits_min_spend]

    rows = [
        ("💵 Annual fee", money(a.annual_fee), money(b.annual_fee)),
        ("🎁 Sign-up bonus value", money(a.bonus_value), money(b.bonus_value)),
        ("💰 Rewards per year", money(a.earn_value), money(b.earn_value)),
        ("🎟️ Credits per year", money(a.benefits_value_steady), money(b.benefits_value_steady)),
        ("📈 Year 1 on its own", signed(a.year1_ev), signed(b.year1_ev)),
        ("📈 Each year after", signed(a.steady_ev), signed(b.steady_ev)),
        ("➕ Year 1 with your wallet", signed(a.marginal_ev_year1), signed(b.marginal_ev_year1)),
        ("➕ Each year after, with your wallet", signed(a.marginal_ev_steady), signed(b.marginal_ev_steady)),
        ("📏 Versus a flat 2% card, per year", signed(a.vs_flat_2pct_steady), signed(b.vs_flat_2pct_steady)),
        ("🧮 Minimum spend fits your usual spending", spend_ok(a), spend_ok(b)),
        ("✅ Eligibility", a.eligibility, b.eligibility),
        ("🔍 Terms verified", verified(card_a), verified(card_b)),
    ]  # fmt: skip
    lines = [f"⚖️ {a.name} vs {b.name}", ""]
    lines += [f"{label}: {left} vs {right}" for label, left, right in rows]

    def winner(field: str, label: str) -> str:
        x, y = getattr(a, field), getattr(b, field)
        if abs(x - y) < 1:
            return f"🏁 {label}: a tie."
        best, gap = (a, x - y) if x > y else (b, y - x)
        return f"🏁 {label}: {best.name}, by {money(gap)}."

    lines += ["", "📊 Year 1 with your wallet"]
    lines += bar_lines(
        [
            (a.marginal_ev_year1, f"{signed(a.marginal_ev_year1)} {a.name}"),
            (b.marginal_ev_year1, f"{signed(b.marginal_ev_year1)} {b.name}"),
        ]
    )
    lines += [
        "",
        winner("marginal_ev_year1", "First year, given your wallet"),
        winner("marginal_ev_steady", "Every year after"),
    ]
    notes = [f"📝 {ev.name}: {note}." for ev in (a, b) for note in ev.notes]
    notes += [
        f"{MARKER} {ev.name}: {terms_warning(card, today)}."
        for ev, card in ((a, card_a), (b, card_b))
        if terms_warning(card, today)
    ]
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


# -------------------------------------------------------------------- wallet
def wallet_text(wallet: list[WalletCard], cards: dict[str, Card]) -> str:
    if not wallet:
        return "👛 Your wallet is empty. Tell me which cards you have and I'll add them."

    def name(card_id: str) -> str:
        return cards[card_id].display_name if card_id in cards else card_id

    lines = ["👛 Your wallet"]
    for w in wallet:
        bits = [
            f"opened {w.opened_on:%b %Y}" if w.opened_on else None,
            f"annual fee posts {w.annual_fee_date:%b %d}" if w.annual_fee_date else None,
            f"bonus received {w.bonus_received_on:%b %Y}" if w.bonus_received_on else None,
            f"closed {w.closed_on:%b %Y}" if w.closed_on else None,
        ]
        detail = ", ".join(b for b in bits if b)
        icon = "💳" if w.is_open else "🗄️"
        lines.append(f"{icon} {name(w.card_id)}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


# -------------------------------------------------------------------- verify
def verify_text(report: Report, card: Card, prequal_url: str | None) -> str:
    lines = [f"🕵️ Verifier check: {report.card_name}", f"{report.headline} ({report.summary})", ""]
    for status in ("fail", "warn", "pass"):
        lines += [check.line for check in report.with_status(status)]
    lines.append("")
    if report.verdict == "fail":
        lines.append("I wouldn't apply for this card right now.")
    else:
        lines += link_lines(report.apply_url, prequal_url, issuer_name(card))
    lines.append("I never apply for you; you decide and apply yourself.")
    return "\n".join(lines)


# -------------------------------------------------------------------- advise
def skipped_line(rejected: list[Report]) -> str:
    """The cards the Verifier turned down, grouped when they share a reason."""
    names = [r.card_name for r in rejected]
    if all(r.with_status("fail")[0].name == "min_spend" for r in rejected):
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        return (
            f"🔁 Skipped {plural(len(names), 'higher-ranked card')} whose minimum spend is above "
            f"your usual spending: {shown}."
        )
    reasons = [
        f"{r.card_name} ({r.with_status('fail')[0].detail.split('.')[0].lower()})"
        for r in rejected[:3]
    ]
    more = f" and {len(rejected) - 3} more" if len(rejected) > 3 else ""
    return f"🔁 Skipped after checking: {', '.join(reasons)}{more}."


def advise_text(outcome: Outcome, cards: dict[str, Card], prequal_url: str | None) -> str:
    if outcome.status != "pick":
        return "\n".join([*(outcome.notes or []), outcome.question or ""]).strip()
    ev, report = outcome.pick, outcome.report
    card = cards[ev.card_id]
    lines = [f"🏆 My pick for you: {ev.name} ({money(ev.annual_fee)} fee)"]
    if ev.offer_summary:
        lines.append(f"🎁 {ev.offer_summary}")
    lines.append(
        f"💰 {signed(ev.marginal_ev_year1)} in year 1 on top of your cards, then "
        f"{signed(ev.marginal_ev_steady)}/yr"
    )
    lines.append(f"🕵️ Checked by my Verifier: {report.summary}")
    lines += [f"   {check.line}" for check in report.with_status("warn")]
    if outcome.rejected:
        lines.append(skipped_line(outcome.rejected))
    lines += link_lines(report.apply_url, prequal_url, issuer_name(card))
    if outcome.runners_up:
        lines += ["", "📊 My pick and the next in my ranking (those aren't checked yet)"]
        shown = [ev, *outcome.runners_up]
        lines += bar_lines(
            [
                (
                    e.marginal_ev_year1,
                    f"{signed(e.marginal_ev_year1)} yr 1 · {signed(e.marginal_ev_steady)}/yr after · {e.name}",
                )
                for e in shown
            ]
        )
    previous = outcome.previous
    if previous:
        when = datetime.fromisoformat(previous["created_at"])
        if previous["card_id"] == ev.card_id:
            lines += ["", f"📌 Same pick as when you asked on {when:%b %d}."]
        else:
            before = previous["detail"].get("name", previous["card_id"])
            lines += ["", f"🔄 Changed since {when:%b %d}, when my pick was {before}."]
    if outcome.notes:
        lines += ["", *outcome.notes]
    lines += [
        "",
        f"🧭 {plural(len(outcome.steps), 'step')}, {plural(outcome.verifier_calls, 'Verifier check')}. "
        'Ask "how did you decide?" to see them.',
        "I never apply for you; you decide and apply yourself.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- trace
def trace_text(records: list[dict]) -> str:
    if not records:
        return "🧭 Nothing in my log yet. Ask me something first."
    lines = [f"🧭 What I did, last {plural(len(records), 'request')} (oldest first)"]
    for entry in records:
        when = datetime.fromisoformat(entry["ts"])
        argv = " ".join(entry.get("argv") or [])
        outcome = "done" if entry.get("ok") else "failed"
        nxt = entry.get("next") or {}
        if nxt.get("action") == "ask_user":
            outcome = "asked you"
        lines += ["", f"🕐 {when:%b %d %H:%M} · {argv} · {entry.get('ms', 0)} ms · {outcome}"]
        steps = entry.get("steps") or []
        if steps:
            lines += [f"   {PHASE_ICON.get(s['phase'], '•')} {s['text']}" for s in steps]
        elif entry.get("summary"):
            lines.append(f"   {entry['summary']}")
    return "\n".join(lines)


# -------------------------------------------------------------------- memory
def memory_text(
    profile: UserProfile,
    spend: dict[Category, float],
    wallet: list[WalletCard],
    hidden: list[dict],
    recommendations: list[dict],
    valuations_set: dict[str, float],
    counts: dict[str, int],
    last_digest: dict | None,
    cards: dict[str, Card],
) -> str:
    def name(card_id: str) -> str:
        return cards[card_id].display_name if card_id in cards else card_id

    weights = profile.goal_weights.normalized()
    goals = ", ".join(
        f"{GOAL_LABEL[goal]} {share:.0%}" for goal, share in weights.items() if share > 0
    )
    lines = ["🧠 What I remember about you (kept privately in your own database)", ""]
    lines.append(f"🎯 Goals: {goals or 'not set'}")
    lines.append(
        f"💵 Annual fee limit {money(profile.max_annual_fee)} · up to "
        f"{plural(profile.max_new_cards_per_year, 'new card')} a year · "
        f"{plural(profile.trips_per_year, 'trip')} a year"
    )
    if profile.credit_score_band or profile.total_credit_limit:
        bits = []
        if profile.credit_score_band:
            bits.append(f"score range {profile.credit_score_band.replace('_', ' ')}")
        if profile.total_credit_limit:
            bits.append(f"total credit limit {money(profile.total_credit_limit)}")
        lines.append(f"🌱 Credit: {', '.join(bits)} (self-reported)")
    if spend:
        total = sum(spend.values())
        parts = ", ".join(
            f"{category_label(category).lower()} {money(amount)}"
            for category, amount in spend.items()
            if amount
        )
        lines.append(f"🛒 Monthly spending {money(total)}: {parts}")
    else:
        lines.append("🛒 Monthly spending: not set yet")
    open_cards = [w for w in wallet if w.is_open]
    closed = [w for w in wallet if not w.is_open]
    if wallet:
        cards_text = ", ".join(
            name(w.card_id) + (f" (since {w.opened_on:%b %Y})" if w.opened_on else "")
            for w in open_cards
        )
        lines.append(f"👛 Wallet: {cards_text or 'no open cards'}")
        if closed:
            lines.append(
                f"🗄️ Closed (kept for issuer rules): {', '.join(name(w.card_id) for w in closed)}"
            )
    else:
        lines.append("👛 Wallet: empty")
    if hidden:
        items = []
        for row in hidden:
            label = (
                name(row["value"])
                if row["kind"] == "card"
                else f"all {ISSUER_DISPLAY.get(row['value'], row['value'])} cards"
            )
            when = datetime.fromisoformat(row["created_at"])
            reason = f', "{row["reason"]}"' if row.get("reason") else ""
            items.append(f"{label} ({when:%b %d}{reason})")
        lines.append(f"🙈 Hidden: {'; '.join(items)}")
    if valuations_set:
        values = ", ".join(f"{currency_label(k)} {v:g}¢" for k, v in valuations_set.items())
        lines.append(f"⚙️ Your point values: {values}")
    if recommendations:
        picks = "; ".join(
            f"{datetime.fromisoformat(r['created_at']):%b %d} {r['detail'].get('name', r['card_id'])}"
            for r in recommendations
        )
        lines.append(f"🏆 Recent picks: {picks}")
    lines.append(
        f"📬 Offers saved from your inbox: {counts.get('personal_offer', 0)} · "
        f"emails checked: {counts.get('processed_message', 0)}"
    )
    if last_digest:
        lines.append(
            f"🗓️ Last digest: {datetime.fromisoformat(last_digest['sent_at']):%b %d} "
            f"({last_digest['channel']})"
        )
    lines += [
        "",
        '✏️ Change anything by telling me, for example "I spend $800 on groceries now", '
        '"I got the Amex Gold", or "show hidden cards again".',
    ]
    return "\n".join(lines)


# -------------------------------------------------------------------- credit
def credit_text(
    health: CreditHealth, prequalify: dict[str, str], resources: list[tuple[str, str]]
) -> str:
    lines = ["🌱 Your credit health check", ""]
    lines += health.notes
    lines += ["", "📊 What goes into a FICO score"]
    lines += bar_lines([(float(weight), f"{weight}% {label}") for label, weight in FICO_FACTORS])
    lines += ["", "✅ Habits that grow a score"]
    lines += [f"{number(i)} {habit}" for i, habit in enumerate(HABITS, 1)]
    if prequalify:
        lines += ["", "🔍 See if you're pre-approved before applying (soft pull, no score impact)"]
        lines += [
            f"• {ISSUER_DISPLAY.get(issuer, issuer)}: {url}" for issuer, url in prequalify.items()
        ]
    lines += ["", "🔗 Free official help"]
    lines += [f"• {title}: {url}" for title, url in resources]
    lines += ["", "Tips, not financial advice. I never pull your score or log in anywhere."]
    return "\n".join(lines)


# ----------------------------------------------------------------------- use
def use_text(rows: list[dict]) -> str:
    if not rows:
        return "👛 Add the cards you have first, then I can tell you which one to use where."
    lines = ["🧾 Which card to use (from your wallet, at your point values)", ""]
    for row in rows:
        category = Category(row["category"])
        best = row["best"]
        line = (
            f"{category_emoji(category)} {category_label(category)}: {best['name']} "
            f"({best['how']}, about {best['cents']:.1f}¢ back per $1)"
        )
        lines.append(line)
        runner = row.get("runner_up")
        if runner and row.get("show_runner_up"):
            lines.append(f"   then {runner['name']} ({runner['how']}, {runner['cents']:.1f}¢)")
    return "\n".join(lines)
