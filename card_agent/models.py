"""Pydantic v2 data model.

Public data (written by the collector into the snapshot on the `data` branch):
Card, EarnRate, SignupOffer, Benefit, Protection, NewsItem, EligibilityRule.
Bonuses and benefits are separate tables: benefits change rarely, bonuses often.

Private state (SQLite only, never committed): UserProfile, MonthlySpend,
PointValuation, UsageHaircut, WalletCard, PersonalOffer.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Category(StrEnum):
    dining = "dining"
    groceries = "groceries"
    online_groceries = "online_groceries"
    gas = "gas"
    ev_charging = "ev_charging"
    travel_portal = "travel_portal"
    travel_general = "travel_general"
    flights = "flights"
    hotels = "hotels"
    transit_rideshare = "transit_rideshare"
    streaming = "streaming"
    drugstores = "drugstores"
    rent = "rent"
    mobile_wallet = "mobile_wallet"
    rotating = "rotating"
    other = "other"


class BenefitKind(StrEnum):
    travel_credit = "travel_credit"
    hotel_credit = "hotel_credit"
    airline_fee = "airline_fee"
    dining_credit = "dining_credit"
    streaming_credit = "streaming_credit"
    rideshare_credit = "rideshare_credit"
    global_entry = "global_entry"
    lounge = "lounge"
    elite_status = "elite_status"
    free_night = "free_night"
    companion_cert = "companion_cert"
    checked_bag = "checked_bag"
    dining_membership = "dining_membership"
    other = "other"


class Cadence(StrEnum):
    monthly = "monthly"
    quarterly = "quarterly"
    semiannual = "semiannual"
    annual = "annual"
    one_time = "one-time"


class ProtectionKind(StrEnum):
    primary_rental = "primary_rental"
    secondary_rental = "secondary_rental"
    trip_delay = "trip_delay"
    trip_cancel = "trip_cancel"
    cell_phone = "cell_phone"
    purchase = "purchase"
    extended_warranty = "extended_warranty"


class BonusUnit(StrEnum):
    points = "points"
    miles = "miles"
    usd = "usd"


CapPeriod = Literal["month", "quarter", "year"]
PERIODS_PER_YEAR: dict[str, int] = {"month": 12, "quarter": 4, "year": 1}


# --------------------------------------------------------------------------
# Public data
# --------------------------------------------------------------------------


class Card(BaseModel):
    id: str
    issuer: str
    name: str
    network: str | None = None
    is_business: bool = False
    annual_fee: float = 0.0
    first_year_fee_waived: bool = False
    foreign_tx_fee: bool | None = None  # None = unknown
    point_currency: str = "usd"
    transferable: bool = False
    apply_url: str | None = None
    discontinued: bool = False
    counts_toward_524: bool | None = None  # None = use the issuer default
    source: str = "bonuses_api"
    source_card_id: str | None = None
    notes: str | None = None

    @property
    def kind(self) -> str:
        return "business" if self.is_business else "personal"

    @property
    def display_name(self) -> str:
        return f"{ISSUER_DISPLAY.get(self.issuer, self.issuer.title())} {self.name}"


class EarnRate(BaseModel):
    card_id: str
    category: Category
    multiplier: float
    cap: float | None = None  # spend cap in USD per cap_period
    cap_period: CapPeriod | None = None
    notes: str | None = None
    # Choice categories (Citi Custom Cash, BofA Customized Cash, US Bank Cash+):
    # rows sharing a choice_group are a menu; the scorer applies the rate to the
    # `choose` categories that are worth the most for this user.
    choice_group: str | None = None
    choose: int | None = None
    source: str = "seed"


class SignupOffer(BaseModel):
    card_id: str
    bonus_amount: float
    bonus_unit: BonusUnit
    min_spend: float | None = None
    spend_window_days: int | None = None
    is_elevated: bool = False
    historical_high: float | None = None
    source: str = "bonuses_api"
    source_url: str | None = None
    fetched_at: datetime
    expires_at: date | None = None
    is_public: bool = True
    extra_usd: float = 0.0  # cash/statement-credit component on a points offer
    extra_points: float = 0.0  # e.g. a free-night certificate, in the card's currency
    details: str | None = None


class Benefit(BaseModel):
    card_id: str
    kind: BenefitKind
    name: str
    face_value_annual: float = 0.0
    value_currency: str = "usd"  # face value unit; points are converted with your cpp
    automatic: bool = False  # e.g. anniversary points: no effort to use, so no haircut
    cadence: Cadence = Cadence.annual
    restrictions: str | None = None
    source: str = "bonuses_api"
    source_weight: float | None = None  # the source's own usability weight, for reference


class Protection(BaseModel):
    card_id: str
    kind: ProtectionKind
    details: str | None = None
    source: str = "seed"


class NewsItem(BaseModel):
    title: str
    url: str
    published_at: datetime
    source: str = "doctorofcredit"
    kind: Literal["new_bonus", "elevated_bonus", "news"] = "news"
    tags: list[str] = Field(default_factory=list)
    card_ids: list[str] = Field(default_factory=list)
    amount: float | None = None
    summary: str | None = None


class CardSelector(BaseModel):
    """Selects cards by issuer, name regex, and/or explicit ids."""

    issuer: str | None = None
    name_pattern: str | None = None
    card_ids: list[str] = Field(default_factory=list)


class EligibilityRule(BaseModel):
    id: str
    issuer: str
    applies_to: CardSelector
    rule_text: str
    # Machine-checkable form: a list of {type: ..., params...}; see eligibility.py.
    checks: list[dict[str, Any]] = Field(default_factory=list)
    source_url: str | None = None


class SourceStatus(BaseModel):
    status: Literal["ok", "error", "disabled", "skipped"]
    url: str | None = None
    fetched_at: datetime | None = None
    count: int = 0
    detail: str | None = None


class Snapshot(BaseModel):
    schema_version: int = 1
    generated_at: datetime
    sources: dict[str, SourceStatus] = Field(default_factory=dict)
    cards: list[Card] = Field(default_factory=list)
    earn_rates: list[EarnRate] = Field(default_factory=list)
    offers: list[SignupOffer] = Field(default_factory=list)
    benefits: list[Benefit] = Field(default_factory=list)
    protections: list[Protection] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    cross_checks: list[dict[str, Any]] = Field(default_factory=list)
    downgrade_paths: dict[str, list[str]] = Field(default_factory=dict)
    changes_file: str | None = None


class ChangeSet(BaseModel):
    date: date
    previous_date: date | None = None
    new_cards: list[dict[str, Any]] = Field(default_factory=list)
    removed_cards: list[dict[str, Any]] = Field(default_factory=list)
    new_offers: list[dict[str, Any]] = Field(default_factory=list)
    removed_offers: list[dict[str, Any]] = Field(default_factory=list)
    elevated_bonuses: list[dict[str, Any]] = Field(default_factory=list)
    reduced_bonuses: list[dict[str, Any]] = Field(default_factory=list)
    fee_changes: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.new_cards,
                self.removed_cards,
                self.new_offers,
                self.removed_offers,
                self.elevated_bonuses,
                self.reduced_bonuses,
                self.fee_changes,
            ]
        )


# --------------------------------------------------------------------------
# Private user state (SQLite)
# --------------------------------------------------------------------------


class GoalWeights(BaseModel):
    travel: float = Field(0.3, ge=0)
    cash_back: float = Field(0.2, ge=0)
    business: float = Field(0.0, ge=0)
    credit_building: float = Field(0.0, ge=0)
    bonus_churning: float = Field(0.5, ge=0)

    def normalized(self) -> dict[str, float]:
        weights = self.model_dump()
        total = sum(weights.values())
        if total <= 0:
            return {key: 0.0 for key in weights}
        return {key: value / total for key, value in weights.items()}


class UserProfile(BaseModel):
    goal_weights: GoalWeights = Field(default_factory=GoalWeights)
    max_annual_fee: float = 700.0
    max_new_cards_per_year: int = 4
    home_airport: str | None = None
    preferred_airlines: list[str] = Field(default_factory=list)
    preferred_hotels: list[str] = Field(default_factory=list)
    elite_statuses: list[str] = Field(default_factory=list)
    trips_per_year: int = 2
    has_business: bool = False
    notify_channels: list[Literal["email", "whatsapp"]] = Field(
        default_factory=lambda: ["email", "whatsapp"]
    )
    notify_day: int = Field(1, ge=1, le=28)
    min_marginal_ev_alert: float = 150.0


class MonthlySpend(BaseModel):
    category: Category
    amount: float = Field(ge=0)


class PointValuation(BaseModel):
    currency: str
    cpp: float = Field(gt=0)  # cents per point


class UsageHaircut(BaseModel):
    """Share of a benefit's face value you actually use (1.0 = all of it, 0 = none)."""

    kind: BenefitKind
    factor: float = Field(ge=0, le=1)


class WalletCard(BaseModel):
    card_id: str
    opened_on: date | None = None
    annual_fee_date: date | None = None
    bonus_received_on: date | None = None
    product_changed_from: str | None = None
    closed_on: date | None = None  # kept for 5/24 and lifetime-rule history

    @property
    def is_open(self) -> bool:
        return self.closed_on is None


class PersonalOffer(BaseModel):
    message_id: str
    received_at: datetime
    kind: Literal["preapproval", "targeted_offer", "offer", "forwarding_confirmation", "other"] = (
        "offer"
    )
    sender: str
    issuer: str | None = None
    card_id: str | None = None
    subject: str = ""
    snippet: str = ""
    bonus_amount: float | None = None
    bonus_unit: BonusUnit | None = None
    min_spend: float | None = None
    spend_window_days: int | None = None
    annual_fee: float | None = None
    expires_at: date | None = None
    suspected_phishing: bool = False
    phishing_reasons: list[str] = Field(default_factory=list)
    confirmation_code: str | None = None

    @field_validator("subject", "snippet")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


# --------------------------------------------------------------------------
# Defaults and display helpers
# --------------------------------------------------------------------------

ISSUER_DISPLAY: dict[str, str] = {
    "amex": "Amex",
    "bofa": "Bank of America",
    "barclays": "Barclays",
    "brex": "Brex",
    "chase": "Chase",
    "capital-one": "Capital One",
    "citi": "Citi",
    "comenity": "Comenity",
    "discover": "Discover",
    "first": "First",
    "fnbo": "FNBO",
    "penfed": "PenFed",
    "pnc": "PNC",
    "synchrony": "Synchrony",
    "us-bank": "U.S. Bank",
    "webbank": "WebBank",
    "wells-fargo": "Wells Fargo",
    "apple": "Apple",
    "robinhood": "Robinhood",
    "bilt": "Bilt",
}

# Cents per point. Conservative, editable via `onboard`.
DEFAULT_VALUATIONS: dict[str, float] = {
    "usd": 1.0,
    "chase_ur": 1.5,
    "amex_mr": 1.5,
    "capital_one_miles": 1.4,
    "citi_typ": 1.4,
    "bilt": 1.5,
    "wells_fargo_rewards": 1.0,
    "us_bank_points": 1.0,
    "bofa_points": 1.0,
    "barclays_points": 1.0,
    "hyatt": 1.7,
    "marriott": 0.7,
    "hilton": 0.5,
    "ihg": 0.5,
    "wyndham": 0.9,
    "choice": 0.6,
    "best_western": 0.6,
    "radisson": 0.3,
    "delta": 1.1,
    "united": 1.2,
    "american": 1.4,
    "southwest": 1.3,
    "alaska": 1.4,
    "jetblue": 1.3,
    "avios": 1.3,
    "aeroplan": 1.4,
    "flying_blue": 1.3,
}
FALLBACK_CPP = 1.0

# Share of face value actually used. Editable via `onboard`.
DEFAULT_HAIRCUTS: dict[BenefitKind, float] = {
    BenefitKind.travel_credit: 0.9,
    BenefitKind.hotel_credit: 0.6,
    BenefitKind.airline_fee: 0.5,
    BenefitKind.dining_credit: 0.6,
    BenefitKind.streaming_credit: 0.5,
    BenefitKind.rideshare_credit: 0.5,
    BenefitKind.global_entry: 0.8,
    BenefitKind.lounge: 0.3,
    BenefitKind.elite_status: 0.3,
    BenefitKind.free_night: 0.7,
    BenefitKind.companion_cert: 0.4,
    BenefitKind.checked_bag: 0.5,
    BenefitKind.dining_membership: 0.3,
    BenefitKind.other: 0.2,
}

# Points currencies that can be transferred to airline/hotel partners.
TRANSFERABLE_CURRENCIES = {
    "chase_ur",
    "amex_mr",
    "capital_one_miles",
    "citi_typ",
    "bilt",
    "wells_fargo_rewards",
}
