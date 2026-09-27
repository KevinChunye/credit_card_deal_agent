"""Evidence rules that need page context: a rate stated once in a heading over a
list ("3x points on:" then "dining ..."), and foreign-fee wording without the
word "foreign". Both stay strict: every quote must be verbatim on the page."""

from __future__ import annotations

from typing import Any

import pytest

from card_agent.terms.page import page_text
from card_agent.terms.pipeline import RunOptions
from card_agent.terms.report import pr_body
from card_agent.terms.schema import TermsExtraction
from card_agent.terms.sources import CardSource, name_matches
from card_agent.terms.validate import (
    heading_item_problem,
    match_key,
    quote_on_page,
    validate_extraction,
)
from tests.terms_fakes import CSP_URL, FakeProvider, earn, make_pipeline, page, sources

HEADING = "3x points on:"
DINING = "dining at restaurants including takeout and eligible delivery services"
VENTURE_X_FTF = (
    "You won’t pay a transaction fee when making purchases outside of the United States."
)

CSP = sources()["chase-sapphire-preferred"]
VENTURE_X = CardSource(
    card_id="capital-one-venture-x",
    issuer="capital-one",
    name="Capital One Venture X",
    url="https://www.capitalone.com/credit-cards/venture-x/",
    page_names=["Venture X"],
)


def listed(category: str, multiplier: float, heading: str, item: str) -> dict[str, Any]:
    return earn(category, multiplier, item, evidence_heading=heading, evidence_item=item)


def extraction(
    *rates: dict[str, Any],
    ftf: dict[str, Any] | None = None,
    name: str = "Chase Sapphire Preferred® Card",
    benefits: tuple[dict[str, Any], ...] = (),
) -> TermsExtraction:
    return TermsExtraction.model_validate(
        {
            "card_name_on_page": name,
            "annual_fee": None,
            "foreign_tx_fee": ftf,
            "point_currency": None,
            "earn_rates": list(rates),
            "benefits": list(benefits),
        }
    )


def validate(page_file: str, found: TermsExtraction, source: CardSource = CSP):
    return validate_extraction(found, page_text(page(page_file)), source, previous=None)


def reasons(result) -> dict[str, str]:
    return {issue.field: issue.reason for issue in result.issues}


# ---------------------------------------------------------------------------
# Heading + list
# ---------------------------------------------------------------------------


def test_rate_stated_in_a_heading_over_a_list_is_accepted():
    result = validate(
        "chase_heading_list.html",
        extraction(
            earn("travel_portal", 5, "5x total points on travel purchased through Chase Travel"),
            listed("gas", 3, HEADING, "gas stations;"),
            listed("dining", 3, HEADING, DINING),
            listed("streaming", 3, HEADING, "top streaming services"),
            listed("online_groceries", 3, HEADING, "online grocery (excluding Target, Walmart"),
            listed("travel_general", 2, "2x points on:", "all other travel purchases"),
        ),
    )
    assert result.issues == []
    rates = {row.category.value: row for row in result.terms.earn}
    assert {k: r.multiplier for k, r in rates.items()} == {
        "travel_portal": 5,
        "gas": 3,
        "dining": 3,
        "streaming": 3,
        "online_groceries": 3,
        "travel_general": 2,
    }
    assert rates["dining"].evidence == f"{HEADING} … {DINING}"
    # A single quote that states the rate needs no heading.
    assert rates["travel_portal"].evidence.startswith("5x total points")


def test_heading_from_a_different_section_is_rejected():
    result = validate(
        "chase_heading_list.html",
        extraction(
            # The 5x line comes before the list, but the "3x points on:" heading sits in between.
            listed("dining", 5, "5x total points on travel purchased through Chase Travel", DINING),
            # "2x points on:" comes after the list; the next mention of the item is in the
            # terms below, with the "3X points" lead-in in between.
            listed("streaming", 2, "2x points on:", "top streaming services"),
            # A real heading that doesn't state the claimed rate.
            listed("gas", 4, HEADING, "gas stations;"),
            earn("travel_portal", 5, "5x total points on travel purchased through Chase Travel"),
        ),
    )
    assert reasons(result) == {
        "earn.dining": "another rate is stated between the heading and the item",
        "earn.streaming": "another rate is stated between the heading and the item",
        "earn.gas": "heading doesn't state 4",
    }


def test_heading_too_far_above_the_item_is_rejected():
    filler = "Terms and conditions apply to this program as described below. " * 30
    page_key = match_key(f"Earn 4x points on: {filler} select streaming services.")
    assert len(filler) > 1500
    assert (
        heading_item_problem(4, "Earn 4x points on:", "select streaming services", page_key)
        == "heading isn't within 1,500 characters before the item"
    )
    near = match_key("Earn 4x points on: select streaming services.")
    assert heading_item_problem(4, "Earn 4x points on:", "select streaming services", near) is None
    assert heading_item_problem(4, "4x", "select streaming services", near) == (
        "heading quote too short"
    )


def test_fabricated_heading_or_item_is_rejected():
    result = validate(
        "chase_heading_list.html",
        extraction(
            listed("groceries", 3, HEADING, "groceries at U.S. supermarkets"),
            listed("drugstores", 4, "4x points on:", "drugstores and pharmacies"),
            # An item that states its own, different rate can't borrow a heading.
            listed(
                "travel_portal", 3, HEADING, "5x total points on travel purchased through Chase"
            ),
            earn("travel_portal", 5, "5x total points on travel purchased through Chase Travel"),
        ),
    )
    assert reasons(result) == {
        "earn.groceries": "list item not found on page",
        "earn.drugstores": "heading not found on page",
        "earn.travel_portal": "list item states a different rate",
    }
    # The fabricated pair shows both quotes in the validation report.
    evidence = {issue.field: issue.evidence for issue in result.issues}
    assert evidence["earn.groceries"] == f"{HEADING} … groceries at U.S. supermarkets"


def test_heading_list_evidence_reaches_the_pr_table():
    provider = FakeProvider(
        {
            "chase-sapphire-preferred": extraction(
                listed("gas", 3, HEADING, "gas stations;"),
                earn("other", 1, "1x point per $1 spent on all other purchases."),
            )
        }
    )
    pipeline, _ = make_pipeline(provider, pages={CSP_URL: page("chase_heading_list.html")})
    report = pipeline.run(RunOptions(mode="full", cards=["chase-sapphire-preferred"]))
    gas = {row.field: row for row in report.diffs}["earn.gas"]
    assert (gas.old, gas.new, gas.evidence) == ("–", "3x", f"{HEADING} … gas stations;")
    assert "“3x points on: … gas stations;”" in pr_body(report, {})


# ---------------------------------------------------------------------------
# Foreign transaction fee wording
# ---------------------------------------------------------------------------


def ftf_result(charged: bool, quote: str):
    return validate(
        "capital_one_venture_x.html",
        extraction(
            ftf={"charged": charged, "evidence": quote},
            name="Capital One Venture X Rewards Credit Card",
        ),
        VENTURE_X,
    )


def test_venture_x_no_fee_sentence_is_accepted():
    result = ftf_result(False, VENTURE_X_FTF)
    assert result.issues == [] and not result.rejected
    assert result.terms.foreign_tx_fee is False
    assert result.terms.evidence["foreign_tx_fee"] == VENTURE_X_FTF


@pytest.mark.parametrize(
    ("charged", "quote", "reason"),
    [
        # Says where, but not that there's (no) fee.
        (False, "Use your card when you travel outside the United States and at home.",
         "evidence doesn't mention a fee or charge"),
        # Says nothing about purchases abroad.
        (False, "Earn unlimited 2X miles on every purchase, every day.",
         "evidence doesn't mention foreign transactions"),
        # The quote says there's no fee, so "charged" is contradicted.
        (True, VENTURE_X_FTF, "evidence contradicts the value"),
        # Not on the page at all.
        (False, "No foreign transaction fees, ever, anywhere in the world.",
         "evidence not found on page"),
    ],
)  # fmt: skip
def test_foreign_fee_quotes_that_are_rejected(charged, quote, reason):
    assert reasons(ftf_result(charged, quote)) == {"foreign_tx_fee": reason}


def test_heading_after_the_item_is_rejected():
    page_key = match_key("dining at restaurants and takeout. 3x points on: gas stations.")
    assert (
        heading_item_problem(3, "3x points on:", "dining at restaurants", page_key)
        == "heading isn't within 1,500 characters before the item"
    )


# ---------------------------------------------------------------------------
# Rules found in the first live runs (quotes are the pages' own sentences)
# ---------------------------------------------------------------------------

LEAD_IN = (
    "3 points (“3X points”): You’ll earn 3 points for each $1 spent on purchases in the "
    "following rewards categories: vacation homes at top brands"
)
AMEX_GOLD = sources()["amex-gold"]


def test_an_earn_quote_must_name_its_category():
    result = validate(
        "chase_heading_list.html",
        extraction(
            # States 3x, but about vacation homes: not evidence for gas.
            earn("gas", 3, LEAD_IN),
            # The same lead-in as a heading over the list item that names the category.
            listed("dining", 3, LEAD_IN, "dining at restaurants including takeout"),
            earn("travel_portal", 5, "5x total points on travel purchased through Chase Travel"),
        ),
    )
    assert reasons(result) == {"earn.gas": "evidence doesn't name gas"}
    dining = next(row for row in result.terms.earn if row.category.value == "dining")
    assert dining.evidence == f"{LEAD_IN} … dining at restaurants including takeout"


def test_portal_only_rates_count_as_travel_portal():
    venture = validate(
        "capital_one_venture_x.html",
        extraction(
            earn(
                "hotels",
                10,
                "Earn 10X miles on hotels and rental cars booked through Capital One Travel.",
            ),
            earn(
                "flights",
                5,
                "Earn 5X miles on flights and vacation rentals booked through Capital One Travel.",
            ),
            earn("other", 2, "Earn unlimited 2X miles on every purchase, every day."),
            name="Capital One Venture X Rewards Credit Card",
        ),
        VENTURE_X,
    )
    assert venture.issues == []
    # Both portal rates land in travel_portal, and the lower one covers every booking.
    rates = {(r.category.value, r.multiplier) for r in venture.terms.earn}
    assert rates == {("travel_portal", 5), ("other", 2)}
    assert any("counted as travel_portal" in note for note in venture.skipped)

    gold = validate(
        "amex_mixed_promo.html",
        extraction(
            earn(
                "hotels",
                5,
                "5X Membership Rewards® points per dollar spent on prepaid hotels booked "
                "through AmexTravel.com",
            ),
            # Also "purchased directly from airlines": a general flights rate.
            earn(
                "flights",
                3,
                "3X Membership Rewards® points per dollar spent on flights booked through "
                "AmexTravel.com or the Amex Travel App™ or purchased directly from airlines.",
            ),
            name="American Express® Gold Card",
        ),
        AMEX_GOLD,
    )
    assert {(r.category.value, r.multiplier) for r in gold.terms.earn} == {
        ("travel_portal", 5),
        ("flights", 3),
    }


def benefit(kind: str, amount: float, cadence: str, quote: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": kind,
        "amount_stated": amount,
        "cadence": cadence,
        "evidence": quote,
    }


def test_benefit_amounts_are_only_ever_lowered():
    venture = validate(
        "capital_one_venture_x.html",
        extraction(
            name="Capital One Venture X Rewards Credit Card",
            benefits=(
                benefit(
                    "other",
                    800,
                    "one-time",
                    "If it’s stolen or damaged, you’ll get reimbursed up to $800.",
                ),
            ),
        ),
        VENTURE_X,
    )
    assert venture.terms.benefits[0].amount is None  # a coverage limit, not a credit
    assert any("coverage limit" in note for note in venture.skipped)

    csp = validate(
        "chase_heading_list.html",
        extraction(
            benefits=(
                benefit(
                    "streaming_credit",
                    156,
                    "annual",
                    "Get a year of complimentary Apple TV when activated by December 31, 2026 - "
                    "a value of $156.",
                ),
                benefit(
                    "dining_membership",
                    120,
                    "annual",
                    "Get a complimentary DashPass membership, a $120 value for 12 months.",
                ),
            ),
        ),
    )
    once = {row.kind.value: (row.amount, row.cadence.value) for row in csp.terms.benefits}
    assert once == {"streaming_credit": (156, "one-time"), "dining_membership": (120, "one-time")}

    gold = validate(
        "amex_mixed_promo.html",
        extraction(
            name="American Express® Gold Card",
            benefits=(
                benefit(
                    "hotel_credit",
                    100,
                    "one-time",
                    "a $100 credit towards eligible charges at over 1,300 upscale hotels "
                    "worldwide every time you book The Hotel Collection",
                ),
                benefit(
                    "rideshare_credit",
                    10,
                    "monthly",
                    "get $10 in Uber Cash each month for U.S. Uber Eats orders",
                ),
                benefit(
                    "other",
                    250,
                    "annual",
                    "get 10% back on qualifying concessions purchases up to $250 per calendar year.",
                ),
            ),
        ),
        AMEX_GOLD,
    )
    amounts = {row.kind.value: row.amount for row in gold.terms.benefits}
    # Per use, or the cap on a 10% rebate: no yearly value. A real monthly credit stays.
    assert amounts == {"hotel_credit": None, "rideshare_credit": 120, "other": None}


# ---------------------------------------------------------------------------
# Rules from the first full bootstrap (each quote is from a real issuer page)
# ---------------------------------------------------------------------------

TEST_CARD = CardSource(
    card_id="test-card", issuer="test", name="Test Card", url="https://bank.example/card",
    page_names=["Test Card"],
)  # fmt: skip


def check_text(
    page: str,
    *rates: dict[str, Any],
    fee: dict[str, Any] | None = None,
    benefits: tuple[dict[str, Any], ...] = (),
):
    found = TermsExtraction.model_validate(
        {
            "card_name_on_page": "Test Card",
            "annual_fee": fee,
            "foreign_tx_fee": None,
            "point_currency": None,
            "earn_rates": list(rates),
            "benefits": list(benefits),
        }
    )
    return validate_extraction(found, f"Test Card. {page}", TEST_CARD, previous=None)


def test_co_brand_and_portal_only_travel_rates():
    marriott = "Card Members can earn 6X points on each dollar of eligible purchases at hotels participating in Marriott Bonvoy."
    aspire = (
        "Card Members can earn 7x Points on purchases of Flights booked directly with airlines "
        "or flights booked through American Express Travel and Car rentals purchases directly "
        "from select car rental companies."
    )
    result = check_text(
        f"{marriott} {aspire}",
        earn("hotels", 6, marriott),
        earn("travel_general", 7, aspire),
        earn("flights", 7, aspire),
    )
    assert reasons(result) == {
        "earn.hotels": "co-brand rate filed as hotels (use not_listed)",
        "earn.travel_general": "evidence doesn't name travel in general",
    }
    assert [(r.category.value, r.multiplier) for r in result.terms.earn] == [("flights", 7)]


def test_capped_category_rates_are_not_the_base_rate_and_caps_are_not_dropped():
    ink = (
        "Earn 5% cash back on the first $25,000 spent in combined purchases at office supply "
        "stores and on internet, cable and phone services each account anniversary year."
    )
    base = "Earn 1% cash back on all other card purchases with no limit to the amount you can earn."
    discover = (
        "Earn 5% cash back on everyday purchases at different places you shop each quarter, "
        "up to the quarterly maximum when you activate."
    )
    result = check_text(
        f"{ink} {base} {discover}",
        earn("other", 5, ink, cap_usd=25000, cap_period="year", cap_evidence=ink),
        earn("other", 1, base),
        earn("rotating", 5, discover),
    )
    assert reasons(result) == {
        "earn.other": "a capped base rate must cover all purchases (use a category or not_listed)",
        "earn.rotating": "evidence mentions a spending cap that wasn't extracted",
    }
    assert [(r.category.value, r.multiplier) for r in result.terms.earn] == [("other", 1)]


def test_menu_options_outside_the_categories_are_left_out():
    menu = (
        "Earn 4X Membership Rewards points on the 2 categories where your business spends the "
        "most in each billing cycle from 6 categories: restaurants, gas stations, transit, "
        "advertising, shipping, and software."
    )

    def option(category: str) -> dict[str, Any]:
        return earn(category, 4, menu, choice_group="top2", choose=2)

    result = check_text(menu, option("dining"), option("gas"), option("other"), option("other"))
    assert sorted(r.category.value for r in result.terms.earn) == ["dining", "gas"]
    assert sum("isn't a spend category" in note for note in result.skipped) == 2


@pytest.mark.parametrize(
    "quote",
    [
        "You won't have to pay an annual fee for all the great features that come with your Freedom Unlimited card.",
        "You won't have to pay an annual credit card fee for all the great features that come with your Prime Visa.",
        "No annual credit card fee",
        "The Citi Double Cash® Card does not charge an annual fee.",
        "No annual, over-the-limit, foreign-transaction, or late fees.",
        "Enjoy all the benefits with no annual fee plus a 0% intro APR on purchases and balance transfers",
    ],
)  # fmt: skip
def test_real_no_annual_fee_wording_is_accepted(quote):
    result = check_text(quote, fee={"amount": 0, "evidence": quote})
    assert result.issues == [] and result.terms.annual_fee == 0


def test_intro_fee_is_still_not_a_zero_fee():
    quote = "$0 intro annual fee for the first year, then $95."
    result = check_text(quote, fee={"amount": 0, "evidence": quote})
    assert reasons(result) == {"annual_fee": "an intro/first-year fee is not the ongoing fee"}


def test_one_word_list_items_must_sit_close_to_the_heading():
    heading = "Earn Unlimited 3X points on :"
    page = (
        f"{heading} Restaurants Travel Gas stations Transit Popular streaming services Phone plans"
    )
    near = check_text(
        page,
        listed("travel_general", 3, heading, "Travel"),
        listed("transit_rideshare", 3, heading, "Transit"),
    )
    assert near.issues == []
    far = check_text(
        f"{heading} Restaurants. " + "Terms and conditions apply to this card. " * 10 + "Travel",
        listed("travel_general", 3, heading, "Travel"),
    )
    assert reasons(far) == {
        "earn.travel_general": "heading isn't within 300 characters before the item"
    }


def test_quote_edges_may_differ_in_punctuation():
    page = match_key(
        "As a Gold Card Member, you are eligible for an upgrade to Hertz Five Star Status and more."
    )
    # A full stop the page doesn't have (the sentence goes on) doesn't matter...
    assert quote_on_page("you are eligible for an upgrade to Hertz Five Star Status.", page)
    # ...but every word still has to be there.
    assert not quote_on_page("you are eligible for an upgrade to Hertz Platinum Status.", page)


# ---------------------------------------------------------------------------
# Rules from the second full bootstrap (real quotes, plus near misses)
# ---------------------------------------------------------------------------


def test_a_capped_rate_on_all_purchases_is_the_base_rate():
    # Blue Business Cash: 2% on everything up to $50,000 a year, then 1%.
    cash = "2% cash back on all eligible purchases on up to $50,000 per calendar year"
    then = "then 1% on eligible purchases thereafter."
    result = check_text(
        f"{cash}, {then}",
        earn("other", 2, cash, cap_usd=50000, cap_period="year", cap_evidence=cash),
        earn("other", 1, then),
    )
    assert result.issues == []
    # The rate after the cap is the overflow the scorer already applies.
    assert [(r.category.value, r.multiplier, r.cap) for r in result.terms.earn] == [
        ("other", 2, 50000)
    ]
    # Blue Business Plus says it without "all".
    plus = "Earn 2X points on the first $50,000 in eligible purchases per calendar year"
    result = check_text(
        plus, earn("other", 2, plus, cap_usd=50000, cap_period="year", cap_evidence=plus)
    )
    assert result.issues == []


@pytest.mark.parametrize(
    ("multiplier", "cap", "quote"),
    [
        (5, 25000, "Earn 5% cash back on all eligible purchases at office supply stores on up to $25,000 per year"),
        (3, 6000, "Earn 3% cash back on eligible purchases on groceries on up to $6,000 per year"),
    ],
)  # fmt: skip
def test_a_capped_rate_on_some_purchases_is_not_the_base_rate(multiplier, cap, quote):
    row = earn("other", multiplier, quote, cap_usd=cap, cap_period="year", cap_evidence=quote)
    assert reasons(check_text(quote, row)) == {
        "earn.other": "a capped base rate must cover all purchases (use a category or not_listed)"
    }


def test_air_travel_and_ground_transportation_name_their_categories():
    strata = "3 points per dollar spent on Air Travel and Other Hotel Purchases"
    heading = "5% cash back on two categories you choose"
    menu = {"choice_group": "five_percent", "choose": 2}
    result = check_text(
        f"{strata}. {heading}: Fast food Ground transportation Home utilities",
        earn("flights", 3, strata),
        earn("hotels", 3, strata),
        listed("transit_rideshare", 5, heading, "Ground transportation") | menu,
        # Fast food is a Cash+ category of its own; dining means restaurants.
        listed("dining", 5, heading, "Fast food") | menu,
    )
    assert reasons(result) == {"earn.dining": "heading and item don't name dining"}
    assert {r.category.value for r in result.terms.earn} == {
        "flights",
        "hotels",
        "transit_rideshare",
    }


def test_travel_rate_limited_to_the_portal_for_airfare_and_hotels():
    # Amex Green: airfare, hotels and car rentals earn 3X only on AmexTravel.com (the
    # rest of the 3X is cruises, tours and third-party sites), so a flight booked
    # with the airline doesn't earn it: travel_portal, not travel_general.
    green = (
        "3X points on travel including airfare, hotels, and car rentals on AmexTravel.com or "
        "the Amex Travel App™, and cruises, tours, campgrounds, vacation rentals, travel "
        "purchases on third party travel websites, and travel purchases on AmexTravel.com."
    )
    result = check_text(green, earn("travel_general", 3, green))
    assert [(r.category.value, r.multiplier) for r in result.terms.earn] == [("travel_portal", 3)]


# ---------------------------------------------------------------------------
# Rules from the third full bootstrap (real quotes)
# ---------------------------------------------------------------------------


def test_what_a_rate_excludes_doesnt_make_it_a_portal_or_co_brand_rate():
    # Sapphire Preferred: 2x travel excludes Chase Travel. Moving it to travel_portal
    # used to replace the 5x portal rate with 2x.
    portal = "You’ll earn 5 points for each $1 spent on purchases made using your card through Chase Travel."
    travel = (
        "You’ll earn 2 points for each $1 spent on purchases made in the travel category "
        "(excluding purchases made through Chase Travel that qualify for 5 points as described)."
    )
    result = check_text(
        f"{portal} {travel}", earn("travel_portal", 5, portal), earn("travel_general", 2, travel)
    )
    assert result.issues == [] and result.skipped == []
    assert {(r.category.value, r.multiplier) for r in result.terms.earn} == {
        ("travel_portal", 5),
        ("travel_general", 2),
    }
    # IHG Premier: travel excludes IHG hotels (10x); that isn't a co-brand travel rate.
    ihg = (
        "You'll earn 5 points for each $1 spent on purchases in the following rewards categories: "
        "travel (excluding purchases made at hotels participating in IHG One Rewards that qualify "
        "for 10 points as described above); gas stations; and dining at restaurants including "
        "takeout and eligible delivery services."
    )
    assert check_text(ihg, earn("travel_general", 5, ihg)).issues == []


def test_the_cards_own_points_dont_make_a_rate_co_brand():
    hyatt = (
        "You’ll earn 2 World of Hyatt Bonus Points for each $1 USD spent on purchases made in any "
        "of the following rewards categories: restaurants (excluding dining purchases that qualify "
        "for 4 Bonus Points as described above); airline tickets when purchased directly with "
        "the airline"
    )
    assert check_text(hyatt, earn("flights", 2, hyatt)).issues == []
    # Where the points are earned still counts.
    marriott = "Earn 6X Marriott Bonvoy points at hotels participating in Marriott Bonvoy."
    assert reasons(check_text(marriott, earn("hotels", 6, marriott))) == {
        "earn.hotels": "co-brand rate filed as hotels (use not_listed)"
    }


def test_airline_status_dollars_are_not_money():
    quote = (
        "Receive $2,500 Medallion Qualification Dollars each Medallion Qualification Year and get "
        "closer to Status with MQD Headstart."
    )
    result = check_text(quote, benefits=(benefit("elite_status", 2500, "annual", quote),))
    assert result.issues == []
    assert [row.amount for row in result.terms.benefits] == [None]
    assert any("status currency" in note for note in result.skipped)


def test_page_name_variants_still_have_to_be_this_card():
    journey = CardSource(
        card_id="wells-fargo-autograph-journey", issuer="wells-fargo",
        name="Wells Fargo Autograph Journey Card", url="https://bank.example/journey",
        page_names=["Autograph Journey"],
    )  # fmt: skip
    # Screen-reader text for the mark is ignored...
    assert name_matches(journey, "Wells Fargo Autograph Journey service mark ℠ Card")
    # ...but a different card is still a different card.
    assert not name_matches(journey, "Wells Fargo Autograph service mark ℠ Card")


@pytest.mark.parametrize(
    ("kind", "amount", "quote"),
    [
        ("travel_credit", 200, "After you spend $10,000 in purchases on your Card in a calendar year, you can receive a $200 Delta Flight Credit to use toward future travel."),
        ("other", 100, "You'll receive a $100 statement credit and 10,000 bonus points each calendar year during which you spend at least $20,000 in purchases."),
        ("travel_credit", 1200, "Unlock up to $1,200 in statement credits on flights booked on AmexTravel.com with the Business Platinum Card, for use in the next calendar year, after spending $250,000 in eligible purchases in this calendar year."),
    ],
)  # fmt: skip
def test_credits_unlocked_by_a_spending_threshold_have_no_dollar_value(kind, amount, quote):
    result = check_text(quote, benefits=(benefit(kind, amount, "annual", quote),))
    assert result.issues == []
    assert [row.amount for row in result.terms.benefits] == [None]
    assert any("spending threshold" in note for note in result.skipped)


def test_a_minimum_purchase_is_not_a_spending_threshold():
    quote = "Get $100 off a single hotel stay of $500 or more when you spend $500 through cititravel.com"
    result = check_text(quote, benefits=(benefit("hotel_credit", 100, "annual", quote),))
    assert [row.amount for row in result.terms.benefits] == [100]


def test_a_spending_threshold_in_the_benefit_name_also_removes_the_dollar_value():
    # Sapphire Reserve: the quote omits the $75,000 threshold the page states elsewhere.
    quote = (
        "statement credits automatically applied to your account for purchases at The Shops "
        "at Chase, up to a maximum accumulation of $250"
    )
    row = benefit("other", 250, "annual", quote) | {
        "name": "The Shops at Chase credit after $75,000 spend"
    }
    result = check_text(quote, benefits=(row,))
    assert [row.amount for row in result.terms.benefits] == [None]
