"""Apply config/card_details.yaml: curated earn rates, foreign transaction
fees, protections, downgrade paths, and a few cards the bonuses API lacks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from card_agent.models import (
    TRANSFERABLE_CURRENCIES,
    Card,
    Category,
    EarnRate,
    Protection,
    ProtectionKind,
)


def load_seed(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


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


def apply_seed(
    seed: dict[str, Any], cards: list[Card], base_rates: list[EarnRate]
) -> tuple[list[Card], list[EarnRate], list[Protection], dict[str, list[str]], list[str]]:
    """Merge the seed into the API cards.

    Returns (cards, earn_rates, protections, downgrade_paths, warnings). Curated
    earn rates replace the API's flat base rate for that card.
    """
    by_id = {card.id: card for card in cards}
    earn_rates: list[EarnRate] = []
    protections: list[Protection] = []
    downgrade_paths: dict[str, list[str]] = {}
    warnings: list[str] = []
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
            warnings.append(f"seed entry {card_id!r} matches no card from the bonuses API")
            continue

        updates: dict[str, Any] = dict(entry.get("override") or {})
        for key in ("foreign_tx_fee", "transferable"):
            if key in entry:
                updates[key] = entry[key]
        if entry.get("notes"):
            updates["notes"] = " ".join(filter(None, [card.notes, entry["notes"]]))
        if updates:
            by_id[card_id] = card.model_copy(update=updates)

        if entry.get("earn"):
            earn_rates.extend(_earn_rows(card_id, entry["earn"]))
            seeded_ids.add(card_id)
        protections.extend(
            Protection(card_id=card_id, kind=ProtectionKind(kind), source="seed")
            for kind in entry.get("protections") or []
        )
        if entry.get("downgrade_to"):
            downgrade_paths[card_id] = list(entry["downgrade_to"])

    earn_rates.extend(rate for rate in base_rates if rate.card_id not in seeded_ids)
    return list(by_id.values()), earn_rates, protections, downgrade_paths, warnings
