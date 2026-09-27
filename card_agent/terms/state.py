"""Pipeline state kept on the `data` branch (never on main):

data/page_hashes.json   per card: page hash, last fetch, last_verified, source_status
data/card_terms.json    per card: the latest validated extraction (with evidence);
                        diffed against config/card_details.yaml on every run, so a
                        proposal stays in the PR until it is merged
data/terms_queue.json   cards queued for forced re-extraction by the RSS trigger
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from card_agent.terms.schema import CardTerms

SourceStatus = Literal["ok", "fetch_failed", "validation_failed", "manual"]
STALE_AFTER_DAYS = 60


class CardState(BaseModel):
    url: str | None = None
    sha256: str | None = None
    last_fetched: date | None = None
    last_verified: date | None = None
    source_status: SourceStatus = "manual"
    extraction_status: Literal["ok", "validation_failed"] | None = None
    last_extracted: date | None = None
    model: str | None = None
    issues: list[dict[str, str]] = Field(default_factory=list)


class PageHashes(BaseModel):
    updated_at: datetime | None = None
    cards: dict[str, CardState] = Field(default_factory=dict)


class CardTermsStore(BaseModel):
    updated_at: datetime | None = None
    cards: dict[str, CardTerms] = Field(default_factory=dict)


class QueueEntry(BaseModel):
    reason: str
    url: str
    queued_at: date


class TermsQueue(BaseModel):
    queued: dict[str, QueueEntry] = Field(default_factory=dict)
    seen_posts: list[str] = Field(default_factory=list)


class StateFiles:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.hashes_path = data_dir / "page_hashes.json"
        self.terms_path = data_dir / "card_terms.json"
        self.queue_path = data_dir / "terms_queue.json"

    def load_hashes(self) -> PageHashes:
        return _load(self.hashes_path, PageHashes)

    def load_terms(self) -> CardTermsStore:
        return _load(self.terms_path, CardTermsStore)

    def load_queue(self) -> TermsQueue:
        return _load(self.queue_path, TermsQueue)

    def save(self, model: BaseModel, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.model_dump_json(indent=1, exclude_none=False))


def _load(path: Path, model):
    if not path.exists():
        return model()
    return model.model_validate_json(path.read_text())


def is_stale(state: CardState | None, today: date) -> bool:
    if state is None or state.source_status != "ok" or state.last_verified is None:
        return True
    return (today - state.last_verified).days > STALE_AFTER_DAYS
