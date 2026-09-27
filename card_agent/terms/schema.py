"""What the LLM must return (sent as a strict JSON schema), and the validated
terms we store and write into config/card_details.yaml.

Every extracted value carries `evidence`: a verbatim quote from the page. The
validator checks each quote against the page text before anything is kept.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from card_agent.models import BenefitKind, Cadence, Category

# The existing spend categories, plus an explicit escape hatch so the model
# never has to force "6x at Marriott" into a general category.
ExtractCategory = StrEnum(
    "ExtractCategory", [(c.value, c.value) for c in Category] + [("not_listed", "not_listed")]
)
CapPeriodOut = Literal["month", "quarter", "year"]


# ---------------------------------------------------------------------------
# LLM output (strict structured-output schema; every field required, nullable
# where it may be absent)
# ---------------------------------------------------------------------------


class AnnualFeeOut(BaseModel):
    amount: float = Field(description="Ongoing annual fee in USD; 0 if the card has none.")
    evidence: str


class ForeignFeeOut(BaseModel):
    charged: bool = Field(description="True if the card charges foreign transaction fees.")
    evidence: str


class CurrencyOut(BaseModel):
    name: str = Field(description="Rewards currency as written, e.g. 'Ultimate Rewards points'.")
    evidence: str


class EarnRateOut(BaseModel):
    category: ExtractCategory
    description: str = Field(description="The category as the page words it.")
    multiplier: float = Field(description="Points/miles per $1, or percent for cash back.")
    cap_usd: float | None = Field(description="Spend cap in USD for this rate, if any.")
    cap_period: CapPeriodOut | None
    cap_evidence: str | None = Field(description="Verbatim quote stating the cap, if any.")
    choice_group: str | None = Field(
        description="Same short label on every option of a 'choose your category' menu."
    )
    choose: int | None = Field(description="How many options of the menu apply at once.")
    evidence: str = Field(
        description="Verbatim quote stating this rate (its multiplier and category)."
    )
    evidence_heading: str | None = Field(
        description=(
            "Only when the page states the multiplier once in a heading over a list of "
            "categories ('3x points on:'): that heading, verbatim. Otherwise null."
        )
    )
    evidence_item: str | None = Field(
        description=(
            "With evidence_heading: the list item naming this category, verbatim. Otherwise null."
        )
    )


class BenefitOut(BaseModel):
    kind: BenefitKind
    name: str
    amount_stated: float | None = Field(
        description="Dollar amount per period as stated (10 for '$10 monthly'); null if none."
    )
    cadence: Cadence
    evidence: str


class TermsExtraction(BaseModel):
    card_name_on_page: str = Field(
        description="The name of the card these terms belong to, exactly as written on the page."
    )
    annual_fee: AnnualFeeOut | None
    foreign_tx_fee: ForeignFeeOut | None
    point_currency: CurrencyOut | None
    earn_rates: list[EarnRateOut]
    benefits: list[BenefitOut]


# ---------------------------------------------------------------------------
# Validated terms (what we store on the data branch and write to the YAML)
# ---------------------------------------------------------------------------


class EarnRow(BaseModel):
    category: Category
    multiplier: float
    cap: float | None = None
    cap_period: CapPeriodOut | None = None
    choice_group: str | None = None
    choose: int | None = None
    notes: str | None = None
    evidence: str | None = None

    def key(self) -> str:
        return (
            f"choice:{self.choice_group}:{self.category}"
            if self.choice_group
            else str(self.category)
        )


class BenefitRow(BaseModel):
    kind: BenefitKind
    name: str
    amount: float | None = None  # USD per year
    cadence: Cadence = Cadence.annual
    evidence: str | None = None


class CardTerms(BaseModel):
    """The generated fields of one card's entry in card_details.yaml."""

    annual_fee: float | None = None
    foreign_tx_fee: bool | None = None
    point_currency: str | None = None
    earn: list[EarnRow] | None = None
    benefits: list[BenefitRow] | None = None
    evidence: dict[str, str] = Field(default_factory=dict)  # scalar field -> quote
