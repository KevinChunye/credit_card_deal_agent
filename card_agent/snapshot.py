"""Read the collector's snapshot from the `data` branch and cache it locally.

Public repo: plain GET of raw.githubusercontent.com. Private repo: set
GITHUB_TOKEN (a read-only fine-grained PAT) and we use the GitHub contents API
with `Accept: application/vnd.github.raw+json`.

Recovery, in order: retry a transient failure (network error, 429, 5xx) once
after a short pause, then switch to the other endpoint (raw <-> contents API),
and if every try fails keep using the saved copy, which is only ever replaced
by a download that validated. `refresh` returns what happened at each try so
the caller can say so and the trace can show it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from pydantic import BaseModel, ValidationError

from card_agent.config import Settings
from card_agent.matching import CardMatcher
from card_agent.models import ChangeSet, SignupOffer, Snapshot

RETRY_STATUS = {429, 500, 502, 503, 504}
TRIES_PER_ENDPOINT = 2
BACKOFF_SECONDS = 1.0
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


PLAIN_REASONS = {
    "ConnectError": "no connection",
    "ProxyError": "no connection",
    "ConnectTimeout": "the connection timed out",
    "ReadTimeout": "the server was too slow",
    "RemoteProtocolError": "the connection dropped",
    "not found": "the data file wasn't found",
    "invalid data": "the download was damaged",
}


class SnapshotMissing(RuntimeError):
    pass


class FetchFailed(RuntimeError):
    """Every endpoint failed. `attempts` lists each try and its error."""

    def __init__(self, path: str, attempts: list[dict[str, Any]]):
        self.attempts = attempts
        tried = ", ".join(f"{a['endpoint']} {a['error']}" for a in attempts)
        super().__init__(f"Couldn't download {path} after {len(attempts)} tries ({tried}).")


def raw_url(settings: Settings, path: str) -> str:
    return f"https://raw.githubusercontent.com/{settings.data_repo}/{settings.data_branch}/{path}"


def api_url(settings: Settings, path: str) -> str:
    return (
        f"https://api.github.com/repos/{settings.data_repo}/contents/{path}"
        f"?ref={settings.data_branch}"
    )


def endpoints(settings: Settings, path: str) -> list[tuple[str, str, dict[str, str]]]:
    """(name, url, headers) in the order to try them."""
    headers = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    api = ("GitHub API", api_url(settings, path), headers)
    raw = ("raw.githubusercontent.com", raw_url(settings, path), {})
    return [api, raw] if settings.github_token else [raw, api]


def fetch_file(
    client: httpx.Client,
    settings: Settings,
    path: str,
    attempts: list[dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """GET one file from the data branch, retrying and switching endpoints.

    A 404 from the first endpoint means the file really isn't there
    (SnapshotMissing). Anything else that fails on every endpoint raises
    FetchFailed. Each failed try is appended to `attempts`.
    """
    attempts = [] if attempts is None else attempts
    for index, (name, url, headers) in enumerate(endpoints(settings, path)):
        for attempt in range(1, TRIES_PER_ENDPOINT + 1):
            try:
                response = client.get(url, headers=headers)
            except httpx.TransportError as exc:
                error = type(exc).__name__
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code == 404 and index == 0:
                    raise SnapshotMissing(
                        f"{path} not found on branch {settings.data_branch!r} of "
                        f"{settings.data_repo}. Has the collector workflow run yet? "
                        "(If the repo is private, set GITHUB_TOKEN.)"
                    )
                error = f"HTTP {response.status_code}"
                if response.status_code not in RETRY_STATUS:
                    attempts.append({"endpoint": name, "try": attempt, "error": error})
                    break  # retrying won't help; switch endpoint
            attempts.append({"endpoint": name, "try": attempt, "error": error})
            if attempt < TRIES_PER_ENDPOINT:
                sleep(BACKOFF_SECONDS * attempt)
    raise FetchFailed(path, attempts)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sync(
    settings: Settings,
    client: httpx.Client,
    from_file: Path | None = None,
    attempts: list[dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Download latest.json (+ its changes file), validate both, then replace the
    cache. A download that fails validation never touches the saved copy."""
    attempts = [] if attempts is None else attempts
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
        latest_bytes = fetch_file(client, settings, "data/latest.json", attempts, sleep)
        snapshot = Snapshot.model_validate_json(latest_bytes)
        changes_bytes = (
            fetch_file(client, settings, snapshot.changes_file, attempts, sleep)
            if snapshot.changes_file
            else None
        )
        origin = f"{settings.data_repo}@{settings.data_branch}"
    if changes_bytes is not None:
        ChangeSet.model_validate_json(changes_bytes)

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(settings.cache_dir / "latest.json", latest_bytes)
    if changes_bytes is not None:
        _write_atomic(settings.cache_dir / "changes.json", changes_bytes)
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
        raise SnapshotMissing(
            "I haven't downloaded any card data yet; it needs a refresh (sync) first."
        )
    return Snapshot.model_validate_json(path.read_text())


def load_changes(settings: Settings) -> ChangeSet | None:
    path = settings.cache_dir / "changes.json"
    return ChangeSet.model_validate_json(path.read_text()) if path.exists() else None


def snapshot_age_days(snapshot: Snapshot, now: datetime) -> float:
    return (now - snapshot.generated_at).total_seconds() / 86400


def saved_age_days(settings: Settings, now: datetime) -> float | None:
    """Age of the saved snapshot in days, or None when there isn't one."""
    try:
        return snapshot_age_days(load_snapshot(settings), now)
    except (SnapshotMissing, ValidationError):
        return None


@dataclass
class Refresh:
    """What a refresh did: fresh data, or why not and whether a saved copy is left."""

    ok: bool
    summary: dict[str, Any] | None = None
    error: str | None = None
    reason: str | None = None  # short: "ConnectError", "HTTP 503", "not found"
    attempts: list[dict[str, Any]] = field(default_factory=list)
    saved_age_days: float | None = None  # the copy in use after a failure

    @property
    def usable(self) -> bool:
        return self.ok or self.saved_age_days is not None

    @property
    def plain_reason(self) -> str:
        """The failure in everyday words, for chat."""
        reason = self.reason or "unknown error"
        if reason.startswith("HTTP "):
            return f"the server answered {reason[5:]}"
        if reason in PLAIN_REASONS:
            return PLAIN_REASONS[reason]
        return "an unexpected error" if reason.endswith(("Error", "Exception")) else reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "fresh" if self.ok else ("saved_copy" if self.usable else "failed"),
            "error": self.error,
            "reason": self.reason,
            "attempts": self.attempts,
            "saved_age_days": None
            if self.saved_age_days is None
            else round(self.saved_age_days, 1),
            **(self.summary or {}),
        }


def refresh(
    settings: Settings,
    now: datetime,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Refresh:
    """sync() with recovery: never raises for network or data problems."""
    attempts: list[dict[str, Any]] = []
    try:
        if client is None:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as own:
                summary = sync(settings, own, attempts=attempts, sleep=sleep)
        else:
            summary = sync(settings, client, attempts=attempts, sleep=sleep)
        return Refresh(ok=True, summary=summary, attempts=attempts)
    except (FetchFailed, SnapshotMissing, httpx.HTTPError, ValueError, OSError) as exc:
        if isinstance(exc, SnapshotMissing):
            reason = "not found"
        elif isinstance(exc, ValidationError):
            reason = "invalid data"
        elif attempts:
            reason = attempts[-1]["error"]
        else:
            reason = type(exc).__name__
        return Refresh(
            ok=False,
            error=str(exc) or type(exc).__name__,
            reason=reason,
            attempts=attempts,
            saved_age_days=saved_age_days(settings, now),
        )


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
        self.downgrade_paths = snapshot.downgrade_paths
