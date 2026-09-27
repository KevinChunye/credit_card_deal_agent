"""Apply config/card_details.yaml: earn rates, annual fees, credits, foreign
transaction fees, rewards currency, protections, downgrade paths, and a few
cards the bonuses API lacks.

For cards the card-terms pipeline tracks, most of these values were read off
the issuer's page (with evidence quotes) and merged through a reviewed PR, so
they take precedence over the bonuses API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from card_agent.models import (
    TRANSFERABLE_CURRENCIES,
    Benefit,
    BenefitKind,
    Cadence,
    Card,
    Category,
    EarnRate,
    Protection,
    ProtectionKind,
)


def load_seed(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


@dataclass
class SeedResult:
    cards: list[Card]
    earn_rates: list[EarnRate]
    protections: list[Protection]
    downgrade_paths: dict[str, list[str]]
    benefits: list[Benefit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _earn_rows(card_id: str, entries: list[dict[str, Any]]) -> list[EarnRate]:
    rows: list[EarnRate] = []
    for index, entry in enumerate(entries):
        common = {
            "card_id": card_id,
            "multiplier": float(entry["multiplier"]),
            "cap": entry.get("cap"),
            "cap_period": entry.get("cap_period"),
            "notes": entry.get("notes"),
            "source": "seed",
        }
        if "choice" in entry:
            group = f"{card_id}:choice{index}"
            rows.extend(
                EarnRate(
                    category=Category(category),
                    choice_group=group,
                    choose=int(entry.get("choose", 1)),
                    **common,
                )
                for category in entry["choice"]
            )
        else:
            rows.append(EarnRate(category=Category(entry["category"]), **common))
    return rows


def _benefit_rows(card_id: str, entries: list[dict[str, Any]]) -> list[Benefit]:
    return [
        Benefit(
            card_id=card_id,
            kind=BenefitKind(entry["kind"]),
            name=entry.get("name", ""),
            face_value_annual=float(entry.get("amount") or 0.0),
            cadence=Cadence(entry.get("cadence", "annual")),
            restrictions=entry.get("evidence"),
            source="seed",
        )
        for entry in entries
    ]


def apply_seed(seed: dict[str, Any], cards: list[Card], base_rates: list[EarnRate]) -> SeedResult:
    """Merge the seed into the API cards. Curated earn rates replace the API's
    flat base rate for that card; curated benefits are merged by run.py."""
    by_id = {card.id: card for card in cards}
    result = SeedResult(cards=[], earn_rates=[], protections=[], downgrade_paths={})
    seeded_ids: set[str] = set()

    for card_id, entry in (seed.get("cards") or {}).items():
        card = by_id.get(card_id)
        if card is None and "define" in entry:
            definition = entry["define"]
            currency = definition.get("point_currency", "usd")
            card = Card(
                id=card_id,
                transferable=currency in TRANSFERABLE_CURRENCIES,
                source="seed",
                **definition,
            )
            by_id[card_id] = card
        if card is None:
            result.warnings.append(f"seed entry {card_id!r} matches no card from the bonuses API")
            continue

        updates: dict[str, Any] = dict(entry.get("override") or {})
        for key in ("foreign_tx_fee", "transferable", "annual_fee", "point_currency"):
            if entry.get(key) is not None:
                updates[key] = entry[key]
        currency = updates.get("point_currency")
        if currency and currency != card.point_currency and "transferable" not in updates:
            updates["transferable"] = currency in TRANSFERABLE_CURRENCIES
        if entry.get("notes"):
            updates["notes"] = " ".join(filter(None, [card.notes, entry["notes"]]))
        if updates:
            by_id[card_id] = card.model_copy(update=updates)

        if entry.get("earn"):
            result.earn_rates.extend(_earn_rows(card_id, entry["earn"]))
            seeded_ids.add(card_id)
        result.benefits.extend(_benefit_rows(card_id, entry.get("benefits") or []))
        result.protections.extend(
            Protection(card_id=card_id, kind=ProtectionKind(kind), source="seed")
            for kind in entry.get("protections") or []
        )
        if entry.get("downgrade_to"):
            result.downgrade_paths[card_id] = list(entry["downgrade_to"])

    result.earn_rates.extend(rate for rate in base_rates if rate.card_id not in seeded_ids)
    result.cards = list(by_id.values())
    return result


def merge_benefits(api: list[Benefit], seed: list[Benefit]) -> list[Benefit]:
    """Issuer-page credits win, kind by kind.

    A seed benefit with a dollar amount replaces the API's benefits of that kind
    for that card. Seed benefits without a dollar amount (lounge access, status)
    don't displace the API's valuation of that kind; they're kept only when the
    API has nothing of that kind.
    """
    valued = {(b.card_id, b.kind) for b in seed if b.face_value_annual > 0}
    api_kinds = {(b.card_id, b.kind) for b in api}
    kept_api = [b for b in api if (b.card_id, b.kind) not in valued]
    unvalued = [
        b for b in seed if b.face_value_annual <= 0 and (b.card_id, b.kind) not in api_kinds
    ]
    return [b for b in seed if b.face_value_annual > 0] + kept_api + unvalued
