"""Diff two snapshots: new/removed cards, new/removed offers, elevated or
reduced bonuses, and annual fee changes."""

from __future__ import annotations

from datetime import date

import pandas as pd

from card_agent.models import ChangeSet, Snapshot


def primary_offers(snapshot: Snapshot) -> pd.DataFrame:
    """One row per card: its best offer, public offers preferred."""
    columns = ["card_id", "bonus_amount", "bonus_unit", "min_spend", "extra_usd", "is_public"]
    if not snapshot.offers:
        return pd.DataFrame(columns=columns).set_index("card_id")
    frame = pd.DataFrame([offer.model_dump(mode="json") for offer in snapshot.offers])[columns]
    frame = frame.sort_values(
        ["card_id", "is_public", "bonus_amount"], ascending=[True, False, False]
    )
    return frame.drop_duplicates("card_id").set_index("card_id")


def _cards(snapshot: Snapshot) -> pd.DataFrame:
    columns = ["id", "issuer", "name", "annual_fee", "discontinued"]
    if not snapshot.cards:
        return pd.DataFrame(columns=columns).set_index("id")
    return pd.DataFrame([card.model_dump() for card in snapshot.cards])[columns].set_index("id")


def diff_snapshots(previous: Snapshot | None, current: Snapshot, today: date) -> ChangeSet:
    if previous is None:
        # First run: this snapshot is the baseline, so nothing counts as "changed".
        return ChangeSet(date=today)

    changes = ChangeSet(date=today, previous_date=previous.generated_at.date())
    old_cards, new_cards = _cards(previous), _cards(current)
    active_new = new_cards[~new_cards["discontinued"].astype(bool)]
    active_old = old_cards[~old_cards["discontinued"].astype(bool)]

    def card_info(card_id: str, frame: pd.DataFrame) -> dict:
        row = frame.loc[card_id]
        return {
            "card_id": card_id,
            "issuer": row["issuer"],
            "name": row["name"],
            "annual_fee": float(row["annual_fee"]),
        }

    changes.new_cards = [
        card_info(card_id, active_new) for card_id in active_new.index.difference(old_cards.index)
    ]
    changes.removed_cards = [
        card_info(card_id, active_old) for card_id in active_old.index.difference(active_new.index)
    ]

    shared = old_cards.index.intersection(new_cards.index)
    fees = pd.DataFrame(
        {"old": old_cards.loc[shared, "annual_fee"], "new": new_cards.loc[shared, "annual_fee"]}
    )
    changed_fees = fees[(fees["old"] - fees["new"]).abs() > 0.5]
    changes.fee_changes = [
        {
            "card_id": card_id,
            "name": new_cards.loc[card_id, "name"],
            "old_annual_fee": float(row.old),
            "new_annual_fee": float(row.new),
        }
        for card_id, row in changed_fees.iterrows()
    ]

    old_offers, new_offers = primary_offers(previous), primary_offers(current)
    for card_id in new_offers.index.difference(old_offers.index):
        if card_id in old_cards.index:  # brand-new cards are reported as new_cards
            changes.new_offers.append(_offer_info(card_id, new_offers.loc[card_id], new_cards))
    for card_id in old_offers.index.difference(new_offers.index):
        if card_id in new_cards.index:
            changes.removed_offers.append(_offer_info(card_id, old_offers.loc[card_id], new_cards))

    both = old_offers.index.intersection(new_offers.index)
    compared = old_offers.loc[both].join(new_offers.loc[both], lsuffix="_old", rsuffix="_new")
    same_unit = compared[compared["bonus_unit_old"] == compared["bonus_unit_new"]]
    for card_id, row in same_unit.iterrows():
        entry = {
            "card_id": card_id,
            "name": new_cards.loc[card_id, "name"] if card_id in new_cards.index else card_id,
            "old_bonus": float(row.bonus_amount_old),
            "new_bonus": float(row.bonus_amount_new),
            "bonus_unit": row.bonus_unit_new,
            "min_spend": None if pd.isna(row.min_spend_new) else float(row.min_spend_new),
        }
        if row.bonus_amount_new > row.bonus_amount_old:
            changes.elevated_bonuses.append(entry)
        elif row.bonus_amount_new < row.bonus_amount_old:
            changes.reduced_bonuses.append(entry)
    return changes


def _offer_info(card_id: str, row: pd.Series, cards: pd.DataFrame) -> dict:
    return {
        "card_id": card_id,
        "name": cards.loc[card_id, "name"] if card_id in cards.index else card_id,
        "bonus_amount": float(row["bonus_amount"]),
        "bonus_unit": row["bonus_unit"],
        "min_spend": None if pd.isna(row["min_spend"]) else float(row["min_spend"]),
    }
