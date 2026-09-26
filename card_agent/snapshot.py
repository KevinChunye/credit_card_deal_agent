"""Read the collector's snapshot from the `data` branch and cache it locally.

Public repo: plain GET of raw.githubusercontent.com. Private repo: set
GITHUB_TOKEN (a read-only fine-grained PAT) and we use the GitHub contents API
with `Accept: application/vnd.github.raw+json`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from pydantic import BaseModel

from card_agent.config import Settings
from card_agent.matching import CardMatcher
from card_agent.models import ChangeSet, SignupOffer, Snapshot


class SnapshotMissing(RuntimeError):
    pass


def raw_url(settings: Settings, path: str) -> str:
    return f"https://raw.githubusercontent.com/{settings.data_repo}/{settings.data_branch}/{path}"


def api_url(settings: Settings, path: str) -> str:
    return (
        f"https://api.github.com/repos/{settings.data_repo}/contents/{path}"
        f"?ref={settings.data_branch}"
    )


def fetch_file(client: httpx.Client, settings: Settings, path: str) -> bytes:
    if settings.github_token:
        response = client.get(
            api_url(settings, path),
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    else:
        response = client.get(raw_url(settings, path))
    if response.status_code == 404:
        raise SnapshotMissing(
            f"{path} not found on branch {settings.data_branch!r} of {settings.data_repo}. "
            "Has the collector workflow run yet? (If the repo is private, set GITHUB_TOKEN.)"
        )
    response.raise_for_status()
    return response.content


def sync(settings: Settings, client: httpx.Client, from_file: Path | None = None) -> dict[str, Any]:
    """Download latest.json (+ its changes file), validate, then replace the cache."""
    if from_file:
        latest_bytes = from_file.read_bytes()
        snapshot = Snapshot.model_validate_json(latest_bytes)
        changes_path = (
            from_file.parent.parent / snapshot.changes_file if snapshot.changes_file else None
        )
        changes_bytes = (
            changes_path.read_bytes() if changes_path and changes_path.exists() else None
        )
        origin = str(from_file)
    else:
        latest_bytes = fetch_file(client, settings, "data/latest.json")
        snapshot = Snapshot.model_validate_json(latest_bytes)
        changes_bytes = (
            fetch_file(client, settings, snapshot.changes_file) if snapshot.changes_file else None
        )
        origin = f"{settings.data_repo}@{settings.data_branch}"
    if changes_bytes is not None:
        ChangeSet.model_validate_json(changes_bytes)

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "latest.json").write_bytes(latest_bytes)
    if changes_bytes is not None:
        (settings.cache_dir / "changes.json").write_bytes(changes_bytes)
    return {
        "origin": origin,
        "generated_at": snapshot.generated_at.isoformat(),
        "cards": len(snapshot.cards),
        "offers": len(snapshot.offers),
        "news": len(snapshot.news),
        "sources": {name: status.status for name, status in snapshot.sources.items()},
        "changes_file": snapshot.changes_file,
    }


def load_snapshot(settings: Settings) -> Snapshot:
    path = settings.cache_dir / "latest.json"
    if not path.exists():
        raise SnapshotMissing("No local snapshot yet. Run `sync` first.")
    return Snapshot.model_validate_json(path.read_text())


def load_changes(settings: Settings) -> ChangeSet | None:
    path = settings.cache_dir / "changes.json"
    return ChangeSet.model_validate_json(path.read_text()) if path.exists() else None


def snapshot_age_days(snapshot: Snapshot, now: datetime) -> float:
    return (now - snapshot.generated_at).total_seconds() / 86400


def group_by_card(items: list[BaseModel]) -> dict[str, list]:
    """card_id -> list of rows, via pandas groupby."""
    if not items:
        return {}
    frame = pd.DataFrame({"card_id": [item.card_id for item in items], "item": items})
    return {card_id: list(group["item"]) for card_id, group in frame.groupby("card_id")}


def best_offers(offers: list[SignupOffer]) -> dict[str, SignupOffer]:
    """card_id -> the best offer: public first, then the largest bonus."""
    if not offers:
        return {}
    frame = pd.DataFrame(
        {
            "card_id": [o.card_id for o in offers],
            "is_public": [o.is_public for o in offers],
            "bonus_amount": [o.bonus_amount + o.extra_usd for o in offers],
            "offer": offers,
        }
    )
    frame = frame.sort_values(
        ["card_id", "is_public", "bonus_amount"], ascending=[True, False, False]
    )
    first = frame.drop_duplicates("card_id")
    return dict(zip(first["card_id"], first["offer"], strict=True))


class DataView:
    """Snapshot tables indexed by card for scoring."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.cards = {card.id: card for card in snapshot.cards}
        self.rates = group_by_card(snapshot.earn_rates)
        self.benefits = group_by_card(snapshot.benefits)
        self.protections = group_by_card(snapshot.protections)
        self.offers = best_offers(snapshot.offers)
        self.matcher = CardMatcher(snapshot.cards)
