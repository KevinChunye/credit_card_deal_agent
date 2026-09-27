"""Source 3 (optional, OFF by default): fuermosi777/rewards.

A static JSON database of cards with earningRates[], benefits[] and
foreignTransactionFee. Useful, but the repository has NO LICENSE file, so no
reuse is granted and the collector does not merge it unless you opt in
(workflow input `with_rewards_db` or repo variable ENABLE_REWARDS_DB=true).
See docs/FINDINGS.md.

When enabled, the workflow does a single `git clone --depth 1` and this module
reads the local files. Merge rules (fill gaps only, never override):
- earn rates: only for cards with no curated rates in config/card_details.yaml
- foreign_tx_fee: only where still unknown
- protections: only for cards with none
- statement credits: only benefit kinds the bonuses API didn't give that card
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from card_agent.collector.bonuses_api import classify_credit, slugify
from card_agent.models import (
    Benefit,
    BenefitKind,
    Cadence,
    Card,
    Category,
    EarnRate,
    Protection,
    ProtectionKind,
)

REPO_URL = "https://github.com/fuermosi777/rewards"

# Their issuerId -> our issuer slug.
ISSUER_MAP = {
    "american-express": "amex",
    "bank-of-america": "bofa",
    "barclays": "barclays",
    "brex": "brex",
    "capital-one": "capital-one",
    "chase": "chase",
    "citi": "citi",
    "comenity": "comenity",
    "discover": "discover",
    "first-national-bank-of-omaha": "fnbo",
    "penfed-credit-union": "penfed",
    "pnc-bank": "pnc",
    "synchrony": "synchrony",
    "u-s-bank": "us-bank",
    "webbank": "webbank",
    "wells-fargo": "wells-fargo",
}

# Their eligibleCategories -> our category enum. Merchant-specific categories
# (e.g. "lyft", "peloton") are skipped on purpose.
CATEGORY_MAP = {
    "dining": Category.dining,
    "gas_stations": Category.gas,
    "grocery_stores": Category.groceries,
    "us_supermarkets": Category.groceries,
    "groceries": Category.groceries,
    "online_groceries": Category.online_groceries,
    "ev_charging": Category.ev_charging,
    "travel": Category.travel_general,
    "hotels": Category.hotels,
    "hotel": Category.hotels,
    "flights": Category.flights,
    "airfare": Category.flights,
    "air_travel": Category.flights,
    "airline": Category.flights,
    "transit": Category.transit_rideshare,
    "local_transit": Category.transit_rideshare,
    "rideshare": Category.transit_rideshare,
    "streaming": Category.streaming,
    "drugstores": Category.drugstores,
    "mobile_wallet": Category.mobile_wallet,
    "everything_else": Category.other,
    "chase_travel": Category.travel_portal,
    "prepaid_hotels_amex_travel": Category.travel_portal,
}
PERIOD_UNITS = {"month": "month", "quarter": "quarter", "year": "year"}

PROTECTION_RULES: list[tuple[str, ProtectionKind]] = [
    (r"primary.*(rental|collision)|(rental|collision).*primary", ProtectionKind.primary_rental),
    (r"rental|collision damage", ProtectionKind.secondary_rental),
    (r"trip delay", ProtectionKind.trip_delay),
    (r"cancell?ation|interruption", ProtectionKind.trip_cancel),
    (r"cell(ular)? phone", ProtectionKind.cell_phone),
    (r"purchase protection", ProtectionKind.purchase),
    (r"extended warranty", ProtectionKind.extended_warranty),
]
CADENCE_BY_UNIT = {"month": Cadence.monthly, "quarter": Cadence.quarterly, "year": Cadence.annual}


def our_card_id(card: dict[str, Any]) -> str | None:
    issuer = ISSUER_MAP.get(card.get("issuerId", ""))
    if issuer is None:
        return None
    return f"{issuer}-{slugify(card.get('name', ''))}"


def load_cards(repo_dir: Path) -> list[dict[str, Any]]:
    cards = []
    for path in sorted((repo_dir / "data" / "cards").glob("*.json")):
        try:
            cards.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return cards


def _earn_rates(card_id: str, raw: dict[str, Any]) -> list[EarnRate]:
    rows = []
    for rate in raw.get("earningRates") or []:
        cap = rate.get("spendCap") or {}
        period = PERIOD_UNITS.get((cap.get("period") or {}).get("unit", ""))
        for category in rate.get("eligibleCategories") or []:
            mapped = CATEGORY_MAP.get(category)
            if mapped is None:
                continue
            rows.append(
                EarnRate(
                    card_id=card_id,
                    category=mapped,
                    multiplier=float(rate["multiplier"]),
                    cap=cap.get("amount"),
                    cap_period=period if cap.get("amount") else None,
                    notes=rate.get("terms"),
                    source="rewards_db",
                )
            )
    return rows


def _benefit(card_id: str, raw: dict[str, Any]) -> Benefit | None:
    detail = raw.get("statementCreditDetail") or {}
    amount = detail.get("amount")
    if not amount:
        return None
    period = raw.get("renewalPeriod") or {}
    unit, count = period.get("unit", "year"), int(period.get("count", 1) or 1)
    per_year = {"month": 12, "quarter": 4, "year": 1}.get(unit, 1) / count
    kind = (
        BenefitKind.global_entry
        if raw.get("benefitType") == "trusted_traveler_credit"
        else classify_credit(f"{raw.get('name', '')} {raw.get('category', '')}")
    )
    return Benefit(
        card_id=card_id,
        kind=kind,
        name=raw.get("name", ""),
        face_value_annual=round(float(amount) * per_year, 2),
        cadence=Cadence.one_time
        if raw.get("isOneTime")
        else (CADENCE_BY_UNIT.get(unit, Cadence.annual) if count == 1 else Cadence.annual),
        restrictions=raw.get("terms") or raw.get("description"),
        source="rewards_db",
    )


def _protections(card_id: str, raw: dict[str, Any]) -> list[Protection]:
    found: dict[ProtectionKind, str] = {}
    for benefit in raw.get("benefits") or []:
        if benefit.get("benefitType") != "insurance":
            continue
        text = (
            f"{benefit.get('name', '')} {benefit.get('description', '')} {benefit.get('terms', '')}"
        )
        for pattern, kind in PROTECTION_RULES:
            if re.search(pattern, text, re.I) and kind not in found:
                found[kind] = benefit.get("description") or benefit.get("name", "")
                break
    return [
        Protection(card_id=card_id, kind=kind, details=details, source="rewards_db")
        for kind, details in found.items()
    ]


def merge(
    repo_dir: Path,
    cards: list[Card],
    earn_rates: list[EarnRate],
    benefits: list[Benefit],
    protections: list[Protection],
) -> tuple[list[Card], list[EarnRate], list[Benefit], list[Protection], int]:
    """Fill gaps from the rewards DB. Returns updated tables and matched-card count."""
    by_id = {card.id: card for card in cards}
    curated = {rate.card_id for rate in earn_rates if rate.source == "seed"}
    protected = {p.card_id for p in protections}
    kinds_by_card: dict[str, set[BenefitKind]] = (
        pd.DataFrame([{"card_id": b.card_id, "kind": b.kind} for b in benefits])
        .groupby("card_id")["kind"]
        .agg(set)
        .to_dict()
        if benefits
        else {}
    )

    matched = 0
    for raw in load_cards(repo_dir):
        card_id = our_card_id(raw)
        if card_id not in by_id:
            continue
        matched += 1
        card = by_id[card_id]
        if card.foreign_tx_fee is None and raw.get("foreignTransactionFee") is not None:
            by_id[card_id] = card.model_copy(
                update={"foreign_tx_fee": raw["foreignTransactionFee"]}
            )
        if card_id not in curated:
            new_rates = _earn_rates(card_id, raw)
            if new_rates:
                earn_rates = [r for r in earn_rates if r.card_id != card_id] + new_rates
        if card_id not in protected:
            protections = protections + _protections(card_id, raw)
        have = kinds_by_card.get(card_id, set())
        for item in raw.get("benefits") or []:
            benefit = _benefit(card_id, item)
            if benefit and benefit.kind not in have:
                benefits = benefits + [benefit]
                have = have | {benefit.kind}
    return list(by_id.values()), earn_rates, benefits, protections, matched
