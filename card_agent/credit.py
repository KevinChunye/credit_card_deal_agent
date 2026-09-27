"""Credit-health check and tips (`credit`).

Deterministic and built only from what you told the agent: your wallet's open
dates, and optionally a self-reported score band and total credit limit. It
never pulls a score, never logs in, and never asks for an SSN.

Factor weights are FICO's published ones, and inquiry timing is myFICO's:
hard inquiries count in FICO scores for 12 months and stay on your reports
for two years (https://www.myfico.com/credit-education/whats-in-your-credit-score).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from card_agent.eligibility import counts_toward_524, months_between
from card_agent.present import plural
from card_agent.scoring import ScoringContext

FICO_FACTORS: list[tuple[str, int]] = [
    ("Payment history", 35),
    ("Amounts owed (utilization)", 30),
    ("Length of credit history", 15),
    ("New credit (inquiries, new accounts)", 10),
    ("Credit mix", 10),
]

BAND_LABEL = {
    "building": "building (no score yet, or under 580)",
    "fair": "fair (580-669)",
    "good": "good (670-739)",
    "very_good": "very good (740-799)",
    "excellent": "excellent (800+)",
}

BAND_NEXT = {
    "building": (
        "Start with a no-annual-fee or secured card, put one small bill on it, and "
        "autopay it in full. Rewards cards can wait."
    ),
    "fair": (
        "Stick to no-annual-fee cards you're likely to get, keep balances low, and let "
        "accounts age. Most premium travel cards want good credit (670+)."
    ),
    "good": (
        "You're in range for most rewards cards. Space out applications so inquiries don't pile up."
    ),
    "very_good": (
        "You're in range for premium cards. The limits that bite now are issuer rules "
        "such as Chase 5/24."
    ),
    "excellent": (
        "You're in range for any card. The limits that bite now are issuer rules such as "
        "Chase 5/24 and how often a bonus can be earned."
    ),
}

HABITS = [
    "Pay every bill on time. Autopay at least the minimum as a safety net.",
    "Keep balances low: under 30% of your limits, under 10% is better. Paying before "
    "the statement closes lowers the balance that gets reported.",
    "Space out applications. Each hard inquiry counts for 12 months and stays on your "
    "report for two years.",
    "Keep your oldest no-fee cards open. If a card gets a fee you don't want, ask for a "
    "product change instead of closing it.",
    "Check your reports free every week at AnnualCreditReport.com and dispute mistakes.",
    "Freeze your credit at all three bureaus while you're not applying. It's free and "
    "blocks new-account fraud.",
]

PACE_DAYS = 90  # a new card younger than this: suggest waiting before the next


@dataclass
class CreditHealth:
    open_cards: int = 0
    closed_cards: int = 0
    undated: int = 0
    new_last_12: int = 0
    chase_524: int = 0
    band: str | None = None
    utilization: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "open_cards": self.open_cards,
            "closed_cards": self.closed_cards,
            "undated_cards": self.undated,
            "new_last_12_months": self.new_last_12,
            "chase_524_count": self.chase_524,
            "credit_score_band": self.band,
            "utilization": None if self.utilization is None else round(self.utilization, 3),
            "notes": self.notes,
        }


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, 28))


def assess(ctx: ScoringContext) -> CreditHealth:
    today = ctx.today
    health = CreditHealth(band=ctx.profile.credit_score_band)
    open_cards = [w for w in ctx.wallet if w.is_open]
    health.open_cards = len(open_cards)
    health.closed_cards = len(ctx.wallet) - len(open_cards)
    new_accounts = [w for w in ctx.wallet if w.opened_on and not w.product_changed_from]
    health.undated = sum(1 for w in ctx.wallet if w.opened_on is None)
    health.new_last_12 = sum(1 for w in new_accounts if (today - w.opened_on).days < 365)
    # Accounts that count for Chase 5/24, oldest first.
    counted = sorted(
        w.opened_on
        for w in new_accounts
        if months_between(w.opened_on, today) < 24 and counts_toward_524(w.card_id, ctx.data.cards)
    )
    health.chase_524 = len(counted)
    notes = health.notes

    # Application pace.
    if new_accounts:
        newest = max(w.opened_on for w in new_accounts)
        newest_card = next(w for w in new_accounts if w.opened_on == newest)
        age_days = (today - newest).days
        if age_days < PACE_DAYS:
            notes.append(
                f"⏳ Your newest card ({ctx.card_name(newest_card.card_id)}) is {age_days} days "
                "old. A common rule of thumb is 3 to 6 months between applications."
            )
        elif health.new_last_12 == 0:
            notes.append(
                "🐢 No new cards in the last 12 months, so no recent inquiries are "
                "weighing on your FICO score."
            )
        limit = ctx.profile.max_new_cards_per_year
        if health.new_last_12 >= limit:
            notes.append(
                f"🛑 {plural(health.new_last_12, 'new card')} in 12 months: that's your own "
                f"limit of {limit}. Consider pausing applications."
            )
        if health.chase_524 >= 5:
            # Back under five once the (n-4) oldest counted accounts pass 24 months.
            frees_up = _add_months(counted[health.chase_524 - 5], 24)
            notes.append(
                f"🔢 Chase 5/24: {health.chase_524} personal cards opened in the last 24 months. "
                f"Chase will likely decline new cards until about {frees_up:%b %Y}."
            )
        else:
            notes.append(
                f"🔢 Chase 5/24: {health.chase_524} of 5 personal cards opened in the last "
                "24 months."
            )

    # Account age.
    dated_open = [w for w in open_cards if w.opened_on]
    if dated_open:
        oldest = min(w.opened_on for w in dated_open)
        oldest_card = next(w for w in dated_open if w.opened_on == oldest)
        years = (today - oldest).days / 365.25
        notes.append(
            f"🏛️ Your oldest open card is {ctx.card_name(oldest_card.card_id)} "
            f"({years:.0f} years, since {oldest:%b %Y}). Keep it open: it anchors your "
            "credit history."
        )
        if len(dated_open) > 1:
            average = sum((today - w.opened_on).days for w in dated_open) / len(dated_open) / 365.25
            notes.append(f"📅 Average age of your open cards: {average:.1f} years.")
    if health.undated:
        notes.append(
            f"📝 {plural(health.undated, 'card')} in your wallet {'has' if health.undated == 1 else 'have'} "
            "no open date. Add it so I can track your pace and account age."
        )

    # Utilization, if we know the limits.
    spend = sum(ctx.spend.values())
    limit_total = ctx.profile.total_credit_limit
    if limit_total and spend:
        health.utilization = spend / limit_total
        pct = f"{health.utilization:.0%}"
        base = f"If your statements show about a month of spending, that's ~{pct} of your ${limit_total:,.0f} in limits."
        if health.utilization >= 0.3:
            notes.append(
                f"⚠️ {base} Above 30% can hurt your score: pay before the statement closes, "
                "or ask for a higher limit."
            )
        elif health.utilization >= 0.1:
            notes.append(f"👍 {base} Fine; under 10% is better (pay before the statement date).")
        else:
            notes.append(f"💪 {base} Excellent.")
    elif spend:
        notes.append(
            "💡 Tell me your total credit limit (all cards added up) and I'll estimate your "
            "utilization, the second biggest part of a FICO score."
        )

    # Next step for the score band.
    if health.band:
        notes.append(f"🎯 Score range {BAND_LABEL[health.band]}: {BAND_NEXT[health.band]}")
    else:
        notes.append(
            "🎯 Tell me your rough score range (building, fair, good, very good or "
            "excellent) and I'll tailor the next step."
        )
    return health
