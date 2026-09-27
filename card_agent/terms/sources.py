"""config/card_sources.yaml: which issuer page each tracked card's terms come from."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from card_agent.config import CONFIG_DIR

SOURCES_PATH = CONFIG_DIR / "card_sources.yaml"


class CardSource(BaseModel):
    card_id: str
    issuer: str
    name: str
    url: str | None = None
    page_names: list[str] = Field(default_factory=list)
    cross_check: bool = False
    manual_reason: str | None = None

    @property
    def is_manual(self) -> bool:
        return self.url is None


def load_sources(path: Path = SOURCES_PATH) -> dict[str, CardSource]:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    return {
        card_id: CardSource(card_id=card_id, **entry)
        for card_id, entry in (data.get("cards") or {}).items()
    }


# Words that say nothing about *which* card it is. Issuer names are dropped too,
# so "Chase Sapphire Preferred® Card" and "Sapphire Preferred" compare equal.
_ISSUER_PHRASES = (
    "american express",
    "amex",
    "chase",
    "capital one",
    "citi",
    "bank of america",
    "wells fargo",
    "u s bank",
    "us bank",
    "barclays",
    "barclaycard",
)
_GENERIC_PHRASES = (
    "world elite mastercard",
    "world elite",
    "world mastercard",
    "visa signature",
    "visa infinite",
    "mastercard",
    "visa",
    "credit card",
    "card",
    "rewards",
    "reward",
    "the",
    "from",
    "by",
)


# Screen-reader text some pages put next to ®/℠/™ ("Autograph Journey service mark ℠ Card").
_MARK_WORDS = re.compile(r"\b(?:registered trademark|trademark|service mark)\b", re.I)


def normalize_card_name(name: str) -> str:
    """Canonical form for comparing card names read off a page."""
    # Strip marks before NFKC, which would turn ™/℠ into the letters "TM"/"SM".
    text = _MARK_WORDS.sub(" ", re.sub(r"[®™℠*†‡]", "", name))
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("+", " plus ").replace("&", " and ")
    text = re.sub(r"\([^)]*\)", " ", text)  # "(Visa Signature)" and similar asides
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    for phrase in (*_ISSUER_PHRASES, *_GENERIC_PHRASES):
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_matches(source: CardSource, name_on_page: str) -> bool:
    """True if the name the model read off the page is one of this card's names."""
    candidate = normalize_card_name(name_on_page)
    names = source.page_names or [source.name]
    return any(candidate == normalize_card_name(name) for name in names)
