"""Source 1: andenacitelli/credit-card-bonuses-api (MIT licensed).

A single static JSON export, fetched once per collector run. Schema (from
src/api.yaml and the real export): a list of CreditCard objects with
cardId, name, issuer, network, currency, isBusiness, annualFee,
isAnnualFeeWaived, universalCashbackPercent, url, imageUrl, credits[],
offers[], historicalOffers[], discontinued, and optional countsTowards524
and details. Offers have spend, amount[{amount, currency?}], days,
credits[], and optional expiration, isPublic, details, url, referralUrl.

The export carries no per-category earn rates and no foreign transaction fee;
those come from config/card_details.yaml (and optionally the rewards DB).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx
import pandas as pd

from card_agent.models import (
    TRANSFERABLE_CURRENCIES,
    Benefit,
    BenefitKind,
    BonusUnit,
    Cadence,
    Card,
    Category,
    EarnRate,
    SignupOffer,
)

DATA_URL = (
    "https://raw.githubusercontent.com/andenacitelli/credit-card-bonuses-api/main/exports/data.json"
)

ISSUER_SLUGS = {
    "AMERICAN_EXPRESS": "amex",
    "BANK_OF_AMERICA": "bofa",
    "BARCLAYS": "barclays",
    "BREX": "brex",
    "CHASE": "chase",
    "CAPITAL_ONE": "capital-one",
    "CITI": "citi",
    "COMENITY": "comenity",
    "DISCOVER": "discover",
    "FIRST": "first",
    "FNBO": "fnbo",
    "PENFED": "penfed",
    "PNC": "pnc",
    "SYNCHRONY": "synchrony",
    "US_BANK": "us-bank",
    "WEB_BANK": "webbank",
    "WELLS_FARGO": "wells-fargo",
}

# The API's CurrenciesEnum -> our valuation keys. Anything not listed is
# lower-cased (DELTA -> delta, HILTON -> hilton, ...).
CURRENCY_KEYS = {
    "USD": "usd",
    "DISCOVER": "usd",
    "CHASE": "chase_ur",
    "AMERICAN_EXPRESS": "amex_mr",
    "CAPITAL_ONE": "capital_one_miles",
    "CITI": "citi_typ",
    "BILT": "bilt",
    "US_BANK": "us_bank_points",
    "BANK_OF_AMERICA": "bofa_points",
    "WELLS_FARGO": "wells_fargo_rewards",
    "BARCLAYS": "barclays_points",
}
AIRLINE_CURRENCIES = {
    "aeroplan",
    "alaska",
    "american",
    "ana",
    "avianca",
    "avios",
    "breeze",
    "cathay_pacific",
    "delta",
    "emirates",
    "flying_blue",
    "frontier",
    "hawaiian",
    "jetblue",
    "korean",
    "latam",
    "lufthansa",
    "southwest",
    "spirit",
    "united",
    "virgin",
    "capital_one_miles",
}

# Credit description -> benefit kind. First match wins, so order matters
# (e.g. "Uber One Membership" is a dining membership, not a rideshare credit).
KIND_RULES: list[tuple[str, BenefitKind]] = [
    (
        r"perks|award (?:flight )?discount|anniversary (?:bonus )?(?:points|miles)",
        BenefitKind.other,
    ),
    (r"precheck|global entry|nexus|trusted traveler", BenefitKind.global_entry),
    (r"lounge|priority pass|centurion", BenefitKind.lounge),
    (r"companion", BenefitKind.companion_cert),
    (r"free night|anniversary night|night award|night certificate", BenefitKind.free_night),
    (r"checked bag|free bag|first bag", BenefitKind.checked_bag),
    (r"status|elite", BenefitKind.elite_status),
    (r"dashpass|uber one|instacart\+|membership", BenefitKind.dining_membership),
    (r"uber|lyft|rideshare", BenefitKind.rideshare_credit),
    (
        r"streaming|digital entertainment|disney|hulu|netflix|apple tv|peacock|spotify|paramount",
        BenefitKind.streaming_credit,
    ),
    (
        r"dining|restaurant|resy|grubhub|doordash|dunkin|shake shack|exclusive tables|opentable",
        BenefitKind.dining_credit,
    ),
    (r"airline|flight|inflight|in-flight", BenefitKind.airline_fee),
    (r"hotel|the edit|resort", BenefitKind.hotel_credit),
    (r"travel", BenefitKind.travel_credit),
]


def fetch(client: httpx.Client, url: str = DATA_URL) -> list[dict[str, Any]]:
    response = client.get(url)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array from {url}, got {type(data).__name__}")
    return data


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def currency_key(raw: str) -> str:
    return CURRENCY_KEYS.get(raw, raw.lower())


def bonus_unit_for(currency: str) -> BonusUnit:
    if currency == "usd":
        return BonusUnit.usd
    return BonusUnit.miles if currency in AIRLINE_CURRENCIES else BonusUnit.points


def classify_credit(description: str) -> BenefitKind:
    lowered = description.lower()
    for pattern, kind in KIND_RULES:
        if re.search(pattern, lowered):
            return kind
    return BenefitKind.other


def cadence_for(description: str) -> Cadence:
    lowered = description.lower()
    if re.search(r"/mo\b|monthly|per month|a month", lowered):
        return Cadence.monthly
    if re.search(r"qtr|quarter", lowered):
        return Cadence.quarterly
    if re.search(r"semi-?annual|2x/yr|twice", lowered):
        return Cadence.semiannual
    return Cadence.annual


def assign_ids(raw_cards: list[dict[str, Any]]) -> dict[str, str]:
    """Map the API's cardId -> our readable id, e.g. `chase-sapphire-preferred`.

    When two cards share an issuer+name (it happens), the lower annual fee keeps
    the plain id and the other gets a short suffix from its cardId, so ids are
    stable run to run.
    """
    frame = pd.DataFrame(
        {
            "source_id": [c["cardId"] for c in raw_cards],
            "base": [
                f"{ISSUER_SLUGS.get(c['issuer'], slugify(c['issuer']))}-{slugify(c['name'])}"
                for c in raw_cards
            ],
            "annual_fee": [float(c.get("annualFee") or 0) for c in raw_cards],
        }
    ).sort_values(["annual_fee", "source_id"])
    ids: dict[str, str] = {}
    taken: set[str] = set()
    for row in frame.itertuples():
        card_id = row.base if row.base not in taken else f"{row.base}-{row.source_id[:6]}"
        taken.add(card_id)
        ids[row.source_id] = card_id
    return ids


AUTOMATIC = re.compile(
    r"anniversary (?:bonus )?(?:points|miles)|anniversary credit|yearly anniversary", re.I
)


def credit_currency(credit: dict[str, Any]) -> str:
    """The unit a credit's `value` is in. The API's optional `currency` is
    sometimes the merchant ("$60 Hilton credit" tagged HILTON), so a "$" in the
    description wins."""
    raw = credit.get("currency")
    if not raw or raw == "USD" or "$" in credit.get("description", ""):
        return "usd"
    return currency_key(raw)


def _offer_amounts(offer: dict[str, Any], card_currency: str) -> tuple[float, float, float]:
    """(headline amount in the card's currency, extra USD, extra points) for one offer.

    Offer credits are extras: USD ones (statement credits, companion vouchers)
    go to extra USD; ones in the card's own currency (a 50k free-night
    certificate) go to extra points; anything else is ignored.
    """
    primary = extra_usd = extra_points = 0.0
    for part in offer.get("amount") or []:
        amount = float(part.get("amount") or 0)
        part_currency = currency_key(part["currency"]) if part.get("currency") else card_currency
        if part_currency == card_currency:
            primary += amount
        elif part_currency == "usd":
            extra_usd += amount
    for credit in offer.get("credits") or []:
        value = float(credit.get("value") or 0)
        unit = credit_currency(credit)
        if unit == "usd":
            extra_usd += value
        elif unit == card_currency:
            extra_points += value
    return primary, extra_usd, extra_points


def normalize(
    raw_cards: list[dict[str, Any]], fetched_at: datetime
) -> tuple[list[Card], list[SignupOffer], list[Benefit], list[EarnRate]]:
    ids = assign_ids(raw_cards)
    cards: list[Card] = []
    offers: list[SignupOffer] = []
    benefits: list[Benefit] = []
    base_rates: list[EarnRate] = []

    for raw in raw_cards:
        card_id = ids[raw["cardId"]]
        currency = currency_key(raw["currency"])
        details = raw.get("details")
        transferable = currency in TRANSFERABLE_CURRENCIES
        if details and re.search(r"non-?transferable|cannot be transferred", details, re.I):
            transferable = False
        cards.append(
            Card(
                id=card_id,
                issuer=ISSUER_SLUGS.get(raw["issuer"], slugify(raw["issuer"])),
                name=raw["name"],
                network=(raw.get("network") or "").lower().replace("_", " ") or None,
                is_business=bool(raw.get("isBusiness")),
                annual_fee=float(raw.get("annualFee") or 0),
                first_year_fee_waived=bool(raw.get("isAnnualFeeWaived")),
                point_currency=currency,
                transferable=transferable,
                apply_url=raw.get("url"),
                discontinued=bool(raw.get("discontinued")),
                counts_toward_524=raw.get("countsTowards524"),
                source="bonuses_api",
                source_card_id=raw["cardId"],
                notes=details,
            )
        )

        # Base earn rate fallback: the API's flat "universal" rate. Curated
        # category rates (config/card_details.yaml) replace this when present.
        universal = raw.get("universalCashbackPercent")
        if universal:
            base_rates.append(
                EarnRate(
                    card_id=card_id,
                    category=Category.other,
                    multiplier=float(universal),
                    notes="base rate from bonuses API universalCashbackPercent",
                    source="bonuses_api",
                )
            )

        for credit in raw.get("credits") or []:
            description = credit.get("description", "").strip()
            kind = classify_credit(description)
            value = float(credit.get("value") or 0)
            restrictions = description
            if kind == BenefitKind.global_entry:
                # ~$100-120 every 4-5 years; annualize so it is comparable.
                value = value / 4
                restrictions = f"{description} (face value every ~4 years, annualized)"
            benefits.append(
                Benefit(
                    card_id=card_id,
                    kind=kind,
                    name=description,
                    face_value_annual=round(value, 2),
                    value_currency=credit_currency(credit),
                    automatic=bool(AUTOMATIC.search(description)),
                    cadence=cadence_for(description),
                    restrictions=restrictions,
                    source="bonuses_api",
                    source_weight=credit.get("weight"),
                )
            )

        historical = [_offer_amounts(o, currency)[0] for o in raw.get("historicalOffers") or []]
        historical = [amount for amount in historical if amount > 0]
        for offer in raw.get("offers") or []:
            primary, extra_usd, extra_points = _offer_amounts(offer, currency)
            if primary <= 0 and extra_usd <= 0 and extra_points <= 0:
                continue
            spend = offer.get("spend")
            expiration = offer.get("expiration")
            offers.append(
                SignupOffer(
                    card_id=card_id,
                    bonus_amount=primary,
                    bonus_unit=bonus_unit_for(currency),
                    min_spend=0.0 if spend is not None and spend < 1 else spend,
                    spend_window_days=int(offer["days"]) if offer.get("days") else None,
                    # "Elevated" = better than the lowest recent offer for this card.
                    is_elevated=bool(historical) and primary > min(historical),
                    historical_high=max([*historical, primary]) if primary > 0 else None,
                    source="bonuses_api",
                    source_url=offer.get("url") or raw.get("url"),
                    fetched_at=fetched_at,
                    expires_at=date.fromisoformat(expiration) if expiration else None,
                    is_public=offer.get("isPublic", True) is not False,
                    extra_usd=extra_usd,
                    extra_points=extra_points,
                    details=offer.get("details"),
                )
            )
    return cards, offers, benefits, base_rates
