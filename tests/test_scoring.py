"""EV math against hand-computed fixtures.

Fixture wallet and spend (monthly): dining $500, groceries $700, other $1,000.
Annual: dining 6,000; groceries 8,400; other 12,000; total 26,400.
"""

from datetime import UTC, date, datetime

import pytest

from card_agent.models import (
    Benefit,
    BenefitKind,
    BonusUnit,
    Cadence,
    Card,
    Category,
    EarnRate,
    SignupOffer,
    Snapshot,
    UserProfile,
    WalletCard,
)
from card_agent.scoring import ScoringContext, evaluate, points_for, rank
from card_agent.snapshot import DataView

NOW = datetime(2026, 9, 26, tzinfo=UTC)
TODAY = date(2026, 9, 26)


def snapshot() -> Snapshot:
    cards = [
        # The card being evaluated: points worth 1.5¢.
        Card(id="t-dining", issuer="test", name="Dining", annual_fee=95, point_currency="pts"),
        # Held: 3% dining, 1% everything else.
        Card(id="t-held", issuer="test", name="Held", annual_fee=0, point_currency="usd"),
        Card(id="t-flat2", issuer="test", name="Flat Two", annual_fee=0, point_currency="usd"),
        # A choice card (Custom Cash style).
        Card(id="t-choice", issuer="test", name="Choice", annual_fee=0, point_currency="usd"),
        # Transfer-locked points in a transferable currency (Freedom Unlimited style).
        Card(
            id="t-locked",
            issuer="test",
            name="Locked",
            point_currency="chase_ur",
            transferable=False,
        ),
        Card(
            id="t-unlock",
            issuer="test",
            name="Unlock",
            point_currency="chase_ur",
            transferable=True,
            annual_fee=95,
        ),
        Card(id="t-biz", issuer="test", name="Biz", is_business=True, point_currency="usd"),
    ]
    rates = [
        EarnRate(card_id="t-dining", category=Category.dining, multiplier=3),
        EarnRate(card_id="t-dining", category=Category.groceries, multiplier=4, cap=6000, cap_period="year"),
        EarnRate(card_id="t-dining", category=Category.other, multiplier=1),
        EarnRate(card_id="t-held", category=Category.dining, multiplier=3),
        EarnRate(card_id="t-held", category=Category.other, multiplier=1),
        EarnRate(card_id="t-flat2", category=Category.other, multiplier=2),
        EarnRate(card_id="t-choice", category=Category.dining, multiplier=5, cap=500, cap_period="month", choice_group="g", choose=1),
        EarnRate(card_id="t-choice", category=Category.groceries, multiplier=5, cap=500, cap_period="month", choice_group="g", choose=1),
        EarnRate(card_id="t-choice", category=Category.other, multiplier=1),
        EarnRate(card_id="t-locked", category=Category.other, multiplier=1.5),
        EarnRate(card_id="t-unlock", category=Category.other, multiplier=1),
        EarnRate(card_id="t-biz", category=Category.other, multiplier=2),
    ]  # fmt: skip
    offers = [
        SignupOffer(
            card_id="t-dining",
            bonus_amount=60000,
            bonus_unit=BonusUnit.points,
            min_spend=4000,
            spend_window_days=90,
            fetched_at=NOW,
        )
    ]
    benefits = [
        Benefit(card_id="t-dining", kind=BenefitKind.dining_credit, name="Dining credit", face_value_annual=120, cadence=Cadence.monthly),
        Benefit(card_id="t-dining", kind=BenefitKind.travel_credit, name="Travel credit", face_value_annual=300),
    ]  # fmt: skip
    return Snapshot(
        generated_at=NOW, cards=cards, earn_rates=rates, offers=offers, benefits=benefits
    )


def context(wallet=None, spend=None, profile=None, haircuts=None, snap=None) -> ScoringContext:
    return ScoringContext(
        data=DataView(snap or snapshot()),
        profile=profile or UserProfile(),
        spend=spend or {Category.dining: 500, Category.groceries: 700, Category.other: 1000},
        valuations={"pts": 1.5, "usd": 1.0, "chase_ur": 1.5},
        haircuts=haircuts or {BenefitKind.dining_credit: 0.5, BenefitKind.travel_credit: 1.0},
        wallet=[WalletCard(card_id="t-held")] if wallet is None else wallet,
        rules=[],
        today=TODAY,
    )


def test_year1_and_steady_state_ev():
    ev = evaluate("t-dining", context())
    # Bonus: 60,000 × 1.5¢ = $900
    assert ev.bonus_value == pytest.approx(900)
    # Dining 6,000 × 3 × 1.5¢ = 270; groceries (6,000 × 4 + 2,400 × 1) × 1.5¢ = 396;
    # other 12,000 × 1 × 1.5¢ = 180. Total 846.
    assert ev.earn_value == pytest.approx(846)
    # Credits: 120 × 0.5 + 300 × 1.0 = 360
    assert ev.benefits_value_year1 == pytest.approx(360)
    assert ev.year1_ev == pytest.approx(900 + 846 + 360 - 95)  # 2,011
    assert ev.steady_ev == pytest.approx(846 + 360 - 95)  # 1,111


def test_marginal_ev_against_wallet():
    ev = evaluate("t-dining", context())
    # Dining: (4.5 - 3.0)¢ × 6,000 = 90. Groceries: 396 - 84 = 312. Other: 0.5¢ × 12,000 = 60.
    incremental = 90 + 312 + 60
    assert ev.marginal_ev_year1 == pytest.approx(900 + incremental + 360 - 95)  # 1,627
    assert ev.marginal_ev_steady == pytest.approx(incremental + 360 - 95)  # 727


def test_marginal_with_empty_wallet_equals_standalone():
    ev = evaluate("t-dining", context(wallet=[]))
    assert ev.marginal_ev_year1 == pytest.approx(ev.year1_ev)


def test_baseline_min_spend_and_bonus_per_dollar():
    ev = evaluate("t-dining", context())
    # Flat 2% on 26,400 = 528
    assert ev.vs_flat_2pct_steady == pytest.approx(1111 - 528)
    assert ev.vs_flat_2pct_year1 == pytest.approx(2011 - 528)
    # 2,200/mo over 90 days ≈ 6,505 ≥ 4,000
    assert ev.hits_min_spend is True
    assert ev.organic_spend_in_window == pytest.approx(2200 * 90 / 30.4375)
    assert ev.bonus_per_min_spend_dollar == pytest.approx(900 / 4000)


def test_min_spend_shortfall_is_flagged():
    ev = evaluate("t-dining", context(spend={Category.dining: 500}))
    assert ev.hits_min_spend is False
    assert any("falls short" in note for note in ev.notes)


def test_duplicate_benefit_kind_counts_zero_in_marginal():
    snap = snapshot()
    snap.benefits.append(
        Benefit(
            card_id="t-held",
            kind=BenefitKind.travel_credit,
            name="Held travel credit",
            face_value_annual=50,
        )
    )
    ev = evaluate("t-dining", context(snap=snap))
    # Travel credit (300) is zeroed in the marginal view; dining credit (60) still counts.
    assert ev.marginal_ev_steady == pytest.approx(462 + 60 - 95)
    line = [line for line in ev.marginal_breakdown if line.label == "Benefit: Travel credit"][0]
    assert line.amount == 0 and "t-held" in line.detail


def test_no_travel_zeroes_travel_benefits():
    snap = snapshot()
    snap.benefits.append(
        Benefit(card_id="t-dining", kind=BenefitKind.lounge, name="Lounge", face_value_annual=400)
    )
    ev = evaluate(
        "t-dining",
        context(
            snap=snap,
            profile=UserProfile(trips_per_year=0),
            haircuts={
                BenefitKind.lounge: 0.5,
                BenefitKind.dining_credit: 0.5,
                BenefitKind.travel_credit: 1.0,
            },
        ),
    )
    lounge = [line for line in ev.breakdown if line.label == "Benefit: Lounge"][0]
    assert lounge.amount == 0


def test_caps_and_choice_categories():
    points, how = points_for(
        Category.groceries,
        8400,
        [
            EarnRate(
                card_id="x", category=Category.groceries, multiplier=4, cap=6000, cap_period="year"
            ),
            EarnRate(card_id="x", category=Category.other, multiplier=1),
        ],
    )
    assert points == 6000 * 4 + 2400 * 1
    assert "first $6,000/yr" in how
    # Choice card: groceries (8,400/yr) and dining (6,000/yr) both max out the $500/mo
    # cap, so the gain ties; the tie-break is more spend in the category, so groceries.
    ev = evaluate("t-choice", context())
    assert any("groceries" in note for note in ev.notes)
    # groceries: 6,000 × 5% + 2,400 × 1% = 324; dining 60; other 120.
    assert ev.earn_value == pytest.approx(324 + 60 + 120)


def test_transfer_locked_points_valued_at_cash_unless_unlocked():
    ev = evaluate("t-locked", context())
    assert ev.cpp == 1.0
    assert any("can't transfer" in note for note in ev.notes)
    unlocked = evaluate("t-locked", context(wallet=[WalletCard(card_id="t-unlock")]))
    assert unlocked.cpp == 1.5


def test_held_card_gets_no_bonus_and_compares_against_rest_of_wallet():
    ev = evaluate(
        "t-dining", context(wallet=[WalletCard(card_id="t-dining"), WalletCard(card_id="t-held")])
    )
    assert ev.held is True
    assert ev.bonus_value == 0
    assert ev.marginal_ev_year1 == pytest.approx(ev.marginal_ev_steady)


def test_rank_sorts_filters_and_excludes_held():
    frame, evaluations = rank(context())
    assert "t-held" not in set(frame["card_id"])
    assert "t-biz" not in set(frame["card_id"])  # personal by default
    assert list(frame["score"]) == sorted(frame["score"], reverse=True)
    assert frame.iloc[0]["card_id"] == "t-dining"
    assert set(rank(context(), mode="business")[0]["card_id"]) == {"t-biz"}
    assert "t-dining" not in set(rank(context(), max_annual_fee=0)[0]["card_id"])
    assert set(rank(context(), mode="cash_back")[0]["point_currency"]) == {"usd"}
    assert "t-held" not in evaluations


def test_rank_skips_discontinued_cards():
    snap = snapshot()
    snap.cards = [
        card.model_copy(update={"discontinued": True}) if card.id == "t-dining" else card
        for card in snap.cards
    ]
    frame, evaluations = rank(context(snap=snap))
    assert "t-dining" not in set(frame["card_id"]) and "t-dining" not in evaluations
