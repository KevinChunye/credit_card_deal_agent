"""Deterministic EV scoring. No LLM anywhere in the math.

Definitions (all in USD per year; cpp = cents per point):
    bonus value     = bonus × cpp/100  (+ any cash component)
    earn value      = Σ_c spend_c × 12 × rate_c × cpp/100   (caps applied per category;
                      spend above a cap earns the card's base "other" rate)
    benefits value  = Σ_i face_i × haircut_i   (haircut = share you actually use)
    Year-1 EV       = bonus + earn + benefits − first-year annual fee
    Steady-state EV = earn + recurring benefits − annual fee
    Marginal EV     = the same, but earn counts only
                      Σ_c spend_c × 12 × max(0, new ¢/$ − best held ¢/$), and benefits
                      whose kind you already get from a held card count as zero.
Every result carries an itemized breakdown so the math can be audited.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

from card_agent.eligibility import EligibilityChecker
from card_agent.models import (
    FALLBACK_CPP,
    PERIODS_PER_YEAR,
    TRANSFERABLE_CURRENCIES,
    Benefit,
    BenefitKind,
    BonusUnit,
    Cadence,
    Card,
    Category,
    EarnRate,
    EligibilityRule,
    UserProfile,
    WalletCard,
)
from card_agent.snapshot import DataView

FLAT_BASELINE_RATE = 0.02  # a no-annual-fee 2% cash back card
DAYS_PER_MONTH = 30.4375

# A card's rate in category X also applies to your spend in these categories.
COVERS: dict[Category, list[Category]] = {
    Category.flights: [Category.flights, Category.travel_general],
    Category.hotels: [Category.hotels, Category.travel_general],
    Category.travel_portal: [Category.travel_portal, Category.travel_general],
    Category.transit_rideshare: [Category.transit_rideshare, Category.travel_general],
    Category.online_groceries: [Category.online_groceries, Category.groceries],
}
# Benefits worth nothing if you don't travel.
TRAVEL_DEPENDENT = {
    BenefitKind.lounge,
    BenefitKind.checked_bag,
    BenefitKind.companion_cert,
    BenefitKind.airline_fee,
    BenefitKind.global_entry,
}


@dataclass
class Line:
    label: str
    amount: float
    detail: str = ""


@dataclass
class ScoringContext:
    data: DataView
    profile: UserProfile
    spend: dict[Category, float]  # monthly USD
    valuations: dict[str, float]
    haircuts: dict[BenefitKind, float]
    wallet: list[WalletCard]
    rules: list[EligibilityRule]
    today: date

    def __post_init__(self) -> None:
        self.open_wallet = [w for w in self.wallet if w.is_open and w.card_id in self.data.cards]
        self.checker = EligibilityChecker(self.rules, self.wallet, self.data.cards, self.today)

    def annual_spend(self, category: Category) -> float:
        return float(self.spend.get(category, 0.0)) * 12


@dataclass
class CardEvaluation:
    card_id: str
    name: str
    issuer: str
    is_business: bool
    annual_fee: float
    point_currency: str
    cpp: float
    held: bool
    bonus_value: float
    earn_value: float
    benefits_value_year1: float
    benefits_value_steady: float
    af_year1: float
    year1_ev: float
    steady_ev: float
    marginal_ev_year1: float
    marginal_ev_steady: float
    vs_flat_2pct_year1: float
    vs_flat_2pct_steady: float
    bonus_per_min_spend_dollar: float | None
    min_spend: float | None
    spend_window_days: int | None
    organic_spend_in_window: float | None
    hits_min_spend: bool | None
    eligibility: str
    eligibility_reasons: list[str]
    goal_fit: float
    score: float
    offer_summary: str | None
    breakdown: list[Line] = field(default_factory=list)
    marginal_breakdown: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            key: (round(value, 2) if isinstance(value, float) else value)
            for key, value in asdict(self).items()
        }


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def effective_cpp(
    card: Card, ctx: ScoringContext, exclude: str | None = None
) -> tuple[float, str | None]:
    """Your valuation for the card's currency, capped at 1.0¢ when the card can't
    transfer points and you hold no card in the same currency that can."""
    if card.point_currency == "usd":
        return 1.0, None
    cpp = float(ctx.valuations.get(card.point_currency, FALLBACK_CPP))
    if card.point_currency in TRANSFERABLE_CURRENCIES and not card.transferable:
        unlocked = False
        for w in ctx.open_wallet:
            held = ctx.data.cards[w.card_id]
            if (
                held.id != exclude
                and held.point_currency == card.point_currency
                and held.transferable
            ):
                unlocked = True
        if not unlocked and cpp > 1.0:
            return 1.0, (
                f"{card.point_currency} valued at 1.0¢ (not {cpp:g}¢): this card can't transfer "
                "points and you hold no card that unlocks transfers"
            )
    return cpp, None


def _base_rate(rates: list[EarnRate]) -> EarnRate | None:
    base = None
    for rate in rates:
        if rate.category == Category.other and (base is None or rate.multiplier > base.multiplier):
            base = rate
    return base


def _annual_cap(rate: EarnRate) -> float | None:
    if rate.cap is None or rate.cap_period is None:
        return None
    return float(rate.cap) * PERIODS_PER_YEAR[rate.cap_period]


def points_for(category: Category, annual: float, rates: list[EarnRate]) -> tuple[float, str]:
    """Points earned on `annual` dollars of spend in `category`, with a short explanation."""
    base = _base_rate(rates)
    base_mult = base.multiplier if base else 1.0
    if category == Category.other:
        best = base
    else:
        covering = [r for r in rates if r.category in COVERS.get(category, [category])]
        best = None
        for rate in covering:
            if rate.multiplier > base_mult and (best is None or rate.multiplier > best.multiplier):
                best = rate
    if best is None:
        note = "" if base else " (no rate data; assumed 1x)"
        return annual * base_mult, f"{base_mult:g}x{note}"
    cap = _annual_cap(best)
    overflow = 1.0 if best is base else base_mult
    if cap is None or annual <= cap:
        return annual * best.multiplier, f"{best.multiplier:g}x"
    points = cap * best.multiplier + (annual - cap) * overflow
    return points, f"{best.multiplier:g}x on first ${cap:,.0f}/yr, then {overflow:g}x"


def resolved_rates(card_id: str, ctx: ScoringContext) -> tuple[list[EarnRate], list[str]]:
    """The card's rates with choice menus resolved to your best picks."""
    rates = ctx.data.rates.get(card_id, [])
    fixed = [r for r in rates if not r.choice_group]
    menu = [r for r in rates if r.choice_group]
    if not menu:
        return fixed, []
    base = _base_rate(fixed)
    base_mult = base.multiplier if base else 1.0
    frame = pd.DataFrame(
        {
            "group": [r.choice_group for r in menu],
            "choose": [r.choose or 1 for r in menu],
            "gain": [
                points_for(r.category, ctx.annual_spend(r.category), [r, *fixed])[0]
                - ctx.annual_spend(r.category) * base_mult
                for r in menu
            ],
            "spend": [ctx.annual_spend(r.category) for r in menu],
            "rate": menu,
        }
    ).sort_values(["group", "gain", "spend"], ascending=[True, False, False])
    picked: list[EarnRate] = []
    notes: list[str] = []
    for _group, group in frame.groupby("group"):
        chosen = list(group.head(int(group["choose"].iloc[0]))["rate"])
        picked.extend(chosen)
        labels = ", ".join(r.category.value for r in chosen)
        notes.append(f"{chosen[0].multiplier:g}x choice category applied to: {labels}")
    return fixed + picked, notes


def category_values(
    card: Card, ctx: ScoringContext, cpp: float
) -> tuple[dict[Category, float], list[Line], list[str]]:
    """USD value of a year of your spend on this card, per category."""
    rates, notes = resolved_rates(card.id, ctx)
    values: dict[Category, float] = {}
    lines: list[Line] = []
    for category in Category:
        annual = ctx.annual_spend(category)
        if annual <= 0:
            continue
        points, how = points_for(category, annual, rates)
        value = points * cpp / 100
        values[category] = value
        lines.append(
            Line(
                f"Earn: {category.value}",
                value,
                f"${annual / 12:,.0f}/mo × 12 × {how} × {cpp:g}¢",
            )
        )
    return values, lines, notes


def benefit_value(benefit: Benefit, ctx: ScoringContext) -> tuple[float, str]:
    haircut = ctx.haircuts.get(benefit.kind, 0.0)
    if benefit.kind in TRAVEL_DEPENDENT and ctx.profile.trips_per_year == 0:
        return 0.0, "you told us you don't travel"
    if benefit.kind == BenefitKind.elite_status:
        programs = {
            status.lower().split()[0] for status in ctx.profile.elite_statuses if status.strip()
        }
        if any(program in benefit.name.lower() for program in programs):
            return 0.0, "you already hold this status"
    return (
        benefit.face_value_annual * haircut,
        f"${benefit.face_value_annual:,.0f} × {haircut:g} usage",
    )


def wallet_benefit_kinds(
    ctx: ScoringContext, exclude: str | None
) -> tuple[dict[BenefitKind, str], set[str]]:
    """Benefit kinds (and 'other' benefit names) you already get from held cards."""
    kinds: dict[BenefitKind, str] = {}
    other_names: set[str] = set()
    for held in ctx.open_wallet:
        if held.card_id == exclude:
            continue
        for benefit in ctx.data.benefits.get(held.card_id, []):
            if benefit.kind == BenefitKind.other:
                other_names.add(benefit.name.lower())
            elif benefit.kind not in kinds:
                kinds[benefit.kind] = held.card_id
    return kinds, other_names


def best_held_cents_per_dollar(
    ctx: ScoringContext, exclude: str | None
) -> dict[Category, tuple[float, str]]:
    """For each category you spend in: the best ¢/$ among cards you hold."""
    best: dict[Category, tuple[float, str]] = {}
    for held in ctx.open_wallet:
        if held.card_id == exclude:
            continue
        card = ctx.data.cards[held.card_id]
        cpp, _ = effective_cpp(card, ctx, exclude=exclude)
        values, _, _ = category_values(card, ctx, cpp)
        for category, value in values.items():
            cents = value * 100 / ctx.annual_spend(category)
            if category not in best or cents > best[category][0]:
                best[category] = (cents, card.id)
    return best


def goal_fit(card: Card, has_bonus: bool, ctx: ScoringContext) -> float:
    weights = ctx.profile.goal_weights.normalized()
    matches = {
        "travel": card.point_currency != "usd",
        "cash_back": card.point_currency == "usd",
        "business": card.is_business,
        "credit_building": card.annual_fee == 0,
        "bonus_churning": has_bonus,
    }
    return sum(weights[goal] for goal, hit in matches.items() if hit)


def horizon_weight(profile: UserProfile) -> float:
    """How much the score leans on year 1 (bonus) vs steady state: 0.5 to 1.0."""
    return 0.5 + 0.5 * profile.goal_weights.normalized()["bonus_churning"]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(card_id: str, ctx: ScoringContext) -> CardEvaluation:
    card = ctx.data.cards[card_id]
    held = any(w.card_id == card_id for w in ctx.open_wallet)
    cpp, cpp_note = effective_cpp(card, ctx)
    notes = [cpp_note] if cpp_note else []
    breakdown: list[Line] = []
    marginal: list[Line] = []

    # Bonus (not counted for a card you already hold).
    offer = None if held else ctx.data.offers.get(card_id)
    bonus_value = 0.0
    offer_summary = None
    if offer:
        points_value = (
            offer.bonus_amount
            if offer.bonus_unit == BonusUnit.usd
            else offer.bonus_amount * cpp / 100
        )
        bonus_value = points_value + offer.extra_usd
        unit = "$" if offer.bonus_unit == BonusUnit.usd else f" {offer.bonus_unit.value}"
        amount = (
            f"${offer.bonus_amount:,.0f}" if unit == "$" else f"{offer.bonus_amount:,.0f}{unit}"
        )
        spend = (
            f" after ${offer.min_spend:,.0f} in {offer.spend_window_days} days"
            if offer.min_spend
            else ""
        )
        offer_summary = f"{amount}{spend}"
        detail = amount if offer.bonus_unit == BonusUnit.usd else f"{amount} × {cpp:g}¢"
        if offer.extra_usd:
            detail += f" + ${offer.extra_usd:,.0f} cash"
        breakdown.append(Line("Sign-up bonus", bonus_value, detail))
        marginal.append(Line("Sign-up bonus", bonus_value, detail))

    # Earn.
    values, earn_lines, choice_notes = category_values(card, ctx, cpp)
    notes += choice_notes
    earn_value = sum(values.values())
    breakdown += earn_lines

    held_best = best_held_cents_per_dollar(ctx, exclude=card_id)
    incremental = 0.0
    for category, value in values.items():
        annual = ctx.annual_spend(category)
        new_cents = value * 100 / annual
        cur_cents, cur_card = held_best.get(category, (0.0, "nothing"))
        gain = annual * max(0.0, new_cents - cur_cents) / 100
        incremental += gain
        marginal.append(
            Line(
                f"Earn: {category.value}",
                gain,
                f"{new_cents:.2f}¢/$ vs {cur_cents:.2f}¢/$ on {cur_card}; ${annual:,.0f}/yr",
            )
        )

    # Benefits.
    have_kinds, have_other = wallet_benefit_kinds(ctx, exclude=card_id)
    benefits_year1 = benefits_steady = 0.0
    marginal_benefits_year1 = marginal_benefits_steady = 0.0
    for benefit in ctx.data.benefits.get(card_id, []):
        value, how = benefit_value(benefit, ctx)
        recurring = benefit.cadence != Cadence.one_time
        benefits_year1 += value
        benefits_steady += value if recurring else 0.0
        breakdown.append(Line(f"Benefit: {benefit.name}", value, how))
        duplicate = (
            have_kinds.get(benefit.kind)
            if benefit.kind != BenefitKind.other
            else ("a held card" if benefit.name.lower() in have_other else None)
        )
        if duplicate:
            marginal.append(
                Line(f"Benefit: {benefit.name}", 0.0, f"already covered by {duplicate}")
            )
            continue
        marginal_benefits_year1 += value
        marginal_benefits_steady += value if recurring else 0.0
        marginal.append(Line(f"Benefit: {benefit.name}", value, how))

    # Fees.
    af_year1 = 0.0 if (card.first_year_fee_waived and not held) else card.annual_fee
    if af_year1:
        breakdown.append(Line("Annual fee (year 1)", -af_year1))
        marginal.append(Line("Annual fee (year 1)", -af_year1))
    elif card.annual_fee:
        breakdown.append(
            Line("Annual fee (year 1)", 0.0, f"waived; ${card.annual_fee:,.0f} from year 2")
        )

    year1 = bonus_value + earn_value + benefits_year1 - af_year1
    steady = earn_value + benefits_steady - card.annual_fee
    marginal_year1 = bonus_value + incremental + marginal_benefits_year1 - af_year1
    marginal_steady = incremental + marginal_benefits_steady - card.annual_fee

    baseline = sum(ctx.annual_spend(c) for c in Category) * FLAT_BASELINE_RATE

    # Minimum spend.
    hits = organic = None
    per_dollar = None
    if offer and offer.min_spend:
        window = offer.spend_window_days or 90
        organic = sum(ctx.spend.values()) * window / DAYS_PER_MONTH
        hits = organic >= offer.min_spend
        per_dollar = bonus_value / offer.min_spend
        if not hits:
            notes.append(
                f"Your usual spend (~${organic:,.0f} in {window} days) falls short of the "
                f"${offer.min_spend:,.0f} minimum; don't manufacture spend to chase it."
            )
    elif offer:
        hits = True

    result = ctx.checker.check(card)
    fit = goal_fit(card, offer is not None, ctx)
    h = horizon_weight(ctx.profile)
    score = h * marginal_year1 + (1 - h) * marginal_steady

    return CardEvaluation(
        card_id=card.id,
        name=card.display_name,
        issuer=card.issuer,
        is_business=card.is_business,
        annual_fee=card.annual_fee,
        point_currency=card.point_currency,
        cpp=cpp,
        held=held,
        bonus_value=bonus_value,
        earn_value=earn_value,
        benefits_value_year1=benefits_year1,
        benefits_value_steady=benefits_steady,
        af_year1=af_year1,
        year1_ev=year1,
        steady_ev=steady,
        marginal_ev_year1=marginal_year1,
        marginal_ev_steady=marginal_steady,
        vs_flat_2pct_year1=year1 - baseline,
        vs_flat_2pct_steady=steady - baseline,
        bonus_per_min_spend_dollar=per_dollar,
        min_spend=offer.min_spend if offer else None,
        spend_window_days=offer.spend_window_days if offer else None,
        organic_spend_in_window=organic,
        hits_min_spend=hits,
        eligibility=result.status,
        eligibility_reasons=result.reasons,
        goal_fit=fit,
        score=score,
        offer_summary=offer_summary,
        breakdown=breakdown,
        marginal_breakdown=marginal,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

MODE_FILTERS = {
    "travel": lambda frame: frame["point_currency"] != "usd",
    "cash_back": lambda frame: frame["point_currency"] == "usd",
    "business": lambda frame: frame["is_business"],
}


def rank(
    ctx: ScoringContext,
    mode: str | None = None,
    kind: str | None = None,
    max_annual_fee: float | None = None,
    include_ineligible: bool = False,
) -> tuple[pd.DataFrame, dict[str, CardEvaluation]]:
    """Evaluate every open-to-apply card and rank by score.

    kind: personal | business | all (default: all if you have a business,
    else personal). max_annual_fee defaults to your profile's limit.
    """
    held_ids = {w.card_id for w in ctx.open_wallet}
    candidates = [
        card_id
        for card_id, card in ctx.data.cards.items()
        if not card.discontinued and card_id not in held_ids
    ]
    evaluations = {card_id: evaluate(card_id, ctx) for card_id in candidates}
    if not evaluations:
        return pd.DataFrame(), {}
    columns = [
        "card_id", "name", "issuer", "is_business", "annual_fee", "point_currency",
        "bonus_value", "year1_ev", "steady_ev", "marginal_ev_year1", "marginal_ev_steady",
        "vs_flat_2pct_steady", "bonus_per_min_spend_dollar", "hits_min_spend",
        "eligibility", "goal_fit", "score", "offer_summary",
    ]  # fmt: skip
    frame = pd.DataFrame(
        [{col: getattr(ev, col) for col in columns} for ev in evaluations.values()]
    )

    kind = kind or ("all" if ctx.profile.has_business or mode == "business" else "personal")
    if kind == "personal":
        frame = frame[~frame["is_business"]]
    elif kind == "business":
        frame = frame[frame["is_business"]]
    limit = ctx.profile.max_annual_fee if max_annual_fee is None else max_annual_fee
    frame = frame[frame["annual_fee"] <= limit]
    if mode:
        frame = frame[MODE_FILTERS[mode](frame)]
    if not include_ineligible:
        frame = frame[frame["eligibility"] != "ineligible"]
    frame = frame.sort_values(
        ["score", "marginal_ev_year1", "goal_fit"], ascending=[False, False, False]
    ).reset_index(drop=True)
    return frame, evaluations


def cards_opened_last_12_months(ctx: ScoringContext) -> int:
    return sum(
        1
        for w in ctx.wallet
        if w.opened_on and not w.product_changed_from and (ctx.today - w.opened_on).days < 365
    )
