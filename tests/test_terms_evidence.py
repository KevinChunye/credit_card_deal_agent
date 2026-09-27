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
from card_agent.terms.sources import CardSource
from card_agent.terms.validate import heading_item_problem, match_key, validate_extraction
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
) -> TermsExtraction:
    return TermsExtraction.model_validate(
        {
            "card_name_on_page": name,
            "annual_fee": None,
            "foreign_tx_fee": ftf,
            "point_currency": None,
            "earn_rates": list(rates),
            "benefits": [],
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
            # "2x points on:" is a heading, but it comes after the item.
            listed("streaming", 2, "2x points on:", "top streaming services"),
            # A real heading that doesn't state the claimed rate.
            listed("gas", 4, HEADING, "gas stations;"),
            earn("travel_portal", 5, "5x total points on travel purchased through Chase Travel"),
        ),
    )
    assert reasons(result) == {
        "earn.dining": "another rate is stated between the heading and the item",
        "earn.streaming": "heading isn't within 1,500 characters before the item",
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
