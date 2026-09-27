"""Shared offline fakes for the card-terms tests: fixture pages, a hand-compiled
card_details stand-in, extractions a model might return, and a fake provider."""

from __future__ import annotations

import re
import threading
from datetime import date
from typing import Any

from card_agent.collector.http import HostThrottle
from card_agent.terms.llm import LLMError, LLMResult, Usage
from card_agent.terms.pipeline import Pipeline, PipelineState
from card_agent.terms.schema import TermsExtraction
from card_agent.terms.sources import CardSource
from tests.conftest import fixture_path, routed_client

TODAY = date(2026, 10, 3)
CSP_URL = "https://creditcards.chase.com/rewards-credit-cards/sapphire/preferred"
GOLD_URL = "https://www.americanexpress.com/us/credit-cards/card/gold-card/"
BILT_URL = "https://www.biltrewards.com/card"


def page(name: str) -> str:
    return fixture_path(f"terms/{name}").read_text()


PAGES = {
    CSP_URL: page("chase_single_card.html"),
    GOLD_URL: page("amex_mixed_promo.html"),
    BILT_URL: page("bilt_lineup.html"),
}


def sources() -> dict[str, CardSource]:
    return {
        "chase-sapphire-preferred": CardSource(
            card_id="chase-sapphire-preferred",
            issuer="chase",
            name="Chase Sapphire Preferred",
            url=CSP_URL,
            page_names=["Sapphire Preferred"],
        ),
        "amex-gold": CardSource(
            card_id="amex-gold",
            issuer="amex",
            name="American Express Gold Card",
            url=GOLD_URL,
            page_names=["Gold", "American Express Gold Card"],
        ),
        "wells-fargo-bilt": CardSource(
            card_id="wells-fargo-bilt",
            issuer="wells-fargo",
            name="Bilt Mastercard",
            url=BILT_URL,
            page_names=["Bilt Mastercard"],
        ),
        "citi-custom-cash": CardSource(
            card_id="citi-custom-cash",
            issuer="citi",
            name="Citi Custom Cash Card",
            url=None,
            page_names=["Custom Cash"],
            manual_reason="citi.com renders this page with JavaScript.",
        ),
    }


def hand_details() -> dict[str, Any]:
    """A hand-compiled card_details.yaml in miniature."""
    return {
        "as_of": "2026-06",
        "cards": {
            "chase-sapphire-preferred": {
                "annual_fee": 95,
                "foreign_tx_fee": False,
                "point_currency": "chase_ur",
                "earn": [
                    {"category": "travel_portal", "multiplier": 5},
                    {"category": "dining", "multiplier": 3},
                    {"category": "streaming", "multiplier": 3},
                    {"category": "travel_general", "multiplier": 2},
                    {"category": "other", "multiplier": 1},
                ],
                "benefits": [
                    {
                        "kind": "hotel_credit",
                        "name": "Chase Travel hotel credit",
                        "amount": 50,
                        "cadence": "annual",
                    }
                ],
                "protections": ["trip_delay", "primary_rental_car"],
                "downgrade_to": ["chase-freedom-unlimited"],
            },
            "amex-gold": {
                "annual_fee": 250,
                "foreign_tx_fee": False,
                "point_currency": "amex_mr",
                "earn": [
                    {"category": "dining", "multiplier": 4, "cap": 50000, "cap_period": "year"},
                    {"category": "groceries", "multiplier": 4, "cap": 25000, "cap_period": "year"},
                    {"category": "flights", "multiplier": 3},
                    {"category": "other", "multiplier": 1},
                ],
                "benefits": [
                    {
                        "kind": "rideshare_credit",
                        "name": "Uber Cash",
                        "amount": 120,
                        "cadence": "annual",
                    }
                ],
            },
            "wells-fargo-bilt": {
                "annual_fee": 0,
                "point_currency": "bilt",
                "earn": [{"category": "rent", "multiplier": 1}],
            },
            "citi-custom-cash": {"annual_fee": 0, "point_currency": "citi_typ"},
        },
    }


def earn(category: str, multiplier: float, evidence: str, **extra: Any) -> dict[str, Any]:
    return {
        "category": category,
        "description": category,
        "multiplier": multiplier,
        "cap_usd": None,
        "cap_period": None,
        "cap_evidence": None,
        "choice_group": None,
        "choose": None,
        "evidence": evidence,
        **extra,
    }


def csp_extraction(**changes: Any) -> TermsExtraction:
    data: dict[str, Any] = {
        "card_name_on_page": "Chase Sapphire Preferred® Card",
        "annual_fee": {"amount": 95, "evidence": "$95 Annual Fee"},
        "foreign_tx_fee": {
            "charged": False,
            "evidence": "You'll pay no foreign transaction fees when you use your card",
        },
        "point_currency": {
            "name": "Ultimate Rewards points",
            "evidence": "Your Ultimate Rewards® points transfer 1:1 to leading airline",
        },
        "earn_rates": [
            earn("travel_portal", 5, "5x total points on travel purchased through Chase Travel℠"),
            earn(
                "dining",
                3,
                "3x points on dining, including eligible delivery services and takeout.",
            ),
            earn("travel_general", 2, "2x points on all other travel purchases."),
            earn("other", 1, "1x point per dollar spent on all other purchases."),
        ],
        "benefits": [
            {
                "kind": "hotel_credit",
                "name": "Chase Travel hotel credit",
                "amount_stated": 50,
                "cadence": "annual",
                "evidence": "Get up to $50 in statement credits each account anniversary year",
            }
        ],
    }
    data.update(changes)
    return TermsExtraction.model_validate(data)


def gold_extraction(**changes: Any) -> TermsExtraction:
    data: dict[str, Any] = {
        "card_name_on_page": "American Express® Gold Card",
        "annual_fee": {"amount": 325, "evidence": "Annual Fee: $325."},
        "foreign_tx_fee": {
            "charged": False,
            "evidence": "No Foreign Transaction Fees on purchases made abroad.",
        },
        "point_currency": {
            "name": "Membership Rewards points",
            "evidence": "Earn 1X Membership Rewards® point per dollar on other eligible purchases.",
        },
        "earn_rates": [
            earn(
                "dining",
                4,
                "Earn 4X Membership Rewards® points at restaurants worldwide",
                cap_usd=50000,
                cap_period="year",
                cap_evidence="on up to $50,000 in purchases per calendar year",
            ),
            earn(
                "groceries",
                4,
                "Earn 4X Membership Rewards® points at U.S. supermarkets",
                cap_usd=25000,
                cap_period="year",
                cap_evidence="on up to $25,000 in purchases per calendar year",
            ),
            earn("flights", 3, "Earn 3X Membership Rewards® points on flights booked directly"),
            earn("other", 1, "Earn 1X Membership Rewards® point per dollar on other eligible"),
            # The Delta promo on the same page: not this card's rate, and not a category.
            earn("not_listed", 2, "Earn 2X Miles on Delta purchases"),
        ],
        "benefits": [
            {
                "kind": "rideshare_credit",
                "name": "Uber Cash",
                "amount_stated": 10,
                "cadence": "monthly",
                "evidence": "get $10 in Uber Cash each month for U.S. Uber Eats orders",
            },
            {
                "kind": "dining_credit",
                "name": "Dining Credit",
                "amount_stated": 10,
                "cadence": "monthly",
                "evidence": "earn up to $10 in statement credits monthly when you pay with the Gold Card",
            },
        ],
    }
    data.update(changes)
    return TermsExtraction.model_validate(data)


class FakeProvider:
    """Returns canned extractions by card id and records every call."""

    name = "fake"

    def __init__(
        self, responses: dict[str, TermsExtraction | Exception], model: str = "gpt-6-luna"
    ):
        self.responses = responses
        self.model = model
        self.calls: list[str] = []
        self.messages: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def extract(self, system: str, user: str, schema: type) -> LLMResult:
        card_id = re.search(r"id: ([\w-]+)\)", user).group(1)
        with self._lock:
            self.calls.append(card_id)
            self.messages.append((system, user))
        response = self.responses[card_id]
        if isinstance(response, Exception):
            raise response
        usage = Usage(calls=1, input_tokens=5_000, output_tokens=1_200, reasoning_tokens=800)
        return LLMResult(parsed=response, usage=usage, model=self.model)


def failing(message: str = "APITimeoutError: timed out") -> LLMError:
    return LLMError(message, Usage(calls=1, input_tokens=5_000))


def make_pipeline(
    provider: FakeProvider | None,
    state: PipelineState | None = None,
    today: date = TODAY,
    pages: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
    provider_note: str | None = None,
) -> tuple[Pipeline, list[str]]:
    client, seen = routed_client(dict(PAGES if pages is None else pages))
    pipeline = Pipeline(
        client=client,
        sources=sources(),
        details=details or hand_details(),
        state=state or PipelineState(),
        today=today,
        provider=provider,
        provider_note=provider_note,
        throttle=HostThrottle(min_interval=0),
    )
    return pipeline, seen
