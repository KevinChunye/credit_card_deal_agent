"""Find card mentions in free text (RSS titles, email subjects, CLI queries).

Deterministic and conservative: a one-word card name ("Gold", "Platinum",
"Cash") only counts when its issuer is also mentioned, and when two names
overlap ("Platinum" inside "Business Platinum") the longer one wins.

`suggest` is the typo fallback ("saphire prefered"): string similarity against
every card's names, used to offer "did you mean" options, or to read a clear
typo as the card it obviously is (read-only commands only; see cli.resolve).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import pandas as pd

from card_agent.models import Card

# A typo is read as a card only if it is this similar to one card's name and
# clearly closer to it than to the runner-up.
CONFIDENT_SIMILARITY = 0.86
CONFIDENT_MARGIN = 0.06
SUGGEST_SIMILARITY = 0.6

ISSUER_ALIASES: dict[str, list[str]] = {
    "amex": ["amex", "american express"],
    "bofa": ["bank of america", "bofa", "boa"],
    "barclays": ["barclays", "barclaycard"],
    "brex": ["brex"],
    "chase": ["chase"],
    "capital-one": ["capital one", "capitalone", "cap one"],
    "citi": ["citi", "citibank"],
    "comenity": ["comenity"],
    "discover": ["discover"],
    "fnbo": ["fnbo", "first national bank of omaha"],
    "penfed": ["penfed"],
    "pnc": ["pnc"],
    "synchrony": ["synchrony"],
    "us-bank": ["us bank", "u s bank", "usbank"],
    "wells-fargo": ["wells fargo", "wf"],
    "apple": ["apple"],
    "robinhood": ["robinhood"],
    "bilt": ["bilt"],
}
# Trailing words that issuers and blogs drop when naming a card.
SUFFIXES = (
    "world elite mastercard",
    "world elite",
    "world mastercard",
    "world",
    "signature",
    "visa signature",
    "visa",
    "mastercard",
    "credit card",
    "card",
)


def normalize(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("+", " plus ")
    text = re.sub(r"[®™*]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {text.strip()} "


def name_variants(name: str) -> set[str]:
    base = normalize(name).strip()
    variants = {base}
    for suffix in SUFFIXES:
        if base.endswith(" " + suffix):
            variants.add(base[: -len(suffix)].strip())
    return {v for v in variants if v}


def issuers_in(text: str) -> set[str]:
    """Issuer slugs whose names appear in `text`."""
    normalized = normalize(text)
    return {
        issuer
        for issuer, aliases in ISSUER_ALIASES.items()
        if any(f" {alias} " in normalized for alias in aliases)
    }


def _span_length(hit: tuple[int, int, str]) -> int:
    return hit[1] - hit[0]


@dataclass(frozen=True)
class _Pattern:
    card_id: str
    issuer: str
    variant: str
    needs_issuer: bool


class CardMatcher:
    def __init__(self, cards: list[Card]):
        self.cards = {card.id: card for card in cards}
        self.patterns: list[_Pattern] = []
        for card in cards:
            for variant in name_variants(card.name):
                # One-word names ("gold", "cash") are too generic on their own.
                needs_issuer = len(variant.split()) < 2
                self.patterns.append(_Pattern(card.id, card.issuer, variant, needs_issuer))

    def find(self, text: str) -> list[str]:
        """Card ids mentioned in `text`, longest non-overlapping names first."""
        normalized = normalize(text)
        issuers = issuers_in(text)
        hits: list[tuple[int, int, str]] = []
        for pattern in self.patterns:
            if pattern.needs_issuer and pattern.issuer not in issuers:
                continue
            for match in re.finditer(re.escape(f" {pattern.variant} "), normalized):
                hits.append((match.start(), match.end(), pattern.card_id))
        # Longest match first; skip any hit overlapping an accepted one.
        hits.sort(key=_span_length, reverse=True)
        accepted: list[tuple[int, int, str]] = []
        for start, end, card_id in hits:
            overlaps = any(
                start < a_end - 1 and a_start < end - 1 for a_start, a_end, _ in accepted
            )
            if not overlaps:
                accepted.append((start, end, card_id))
        # Several issuers can share a name ("Premier"); keep only issuer-consistent
        # hits when the text names an issuer.
        found = []
        for _start, _end, card_id in accepted:
            issuer = self.cards[card_id].issuer
            if issuers and issuer not in issuers:
                continue
            if card_id not in found:
                found.append(card_id)
        return found

    def resolve(self, query: str) -> tuple[str | None, list[str]]:
        """Resolve a user-typed card reference to one id.

        Returns (card_id, candidates). card_id is None when nothing or more
        than one card matches; candidates then lists the options.
        """
        query = query.strip()
        if query in self.cards:
            return query, [query]
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        if slug in self.cards:
            return slug, [slug]
        found = self.find(query)
        if len(found) == 1:
            return found[0], found
        if found:
            return None, found
        tokens = normalize(query).split()
        candidates = [
            card.id
            for card in self.cards.values()
            if all(token in normalize(f"{card.issuer} {card.name} {card.id}") for token in tokens)
        ]
        if len(candidates) == 1:
            return candidates[0], candidates
        return None, candidates

    def suggest(self, query: str, limit: int = 3) -> pd.DataFrame:
        """Cards whose names look like `query`, most similar first
        (columns: card_id, similarity). For typos, not for exact names."""
        wanted = normalize(query).strip()
        rows = []
        for card in self.cards.values():
            names = name_variants(card.name) | {
                normalize(f"{issuer} {card.name}").strip()
                for issuer in ISSUER_ALIASES.get(card.issuer, [card.issuer])
            }
            best = max(SequenceMatcher(None, wanted, name).ratio() for name in names)
            rows.append({"card_id": card.id, "similarity": best})
        frame = pd.DataFrame(rows, columns=["card_id", "similarity"])
        frame = frame[frame["similarity"] >= SUGGEST_SIMILARITY]
        return frame.sort_values(["similarity", "card_id"], ascending=[False, True]).head(limit)

    def confident_guess(self, query: str) -> str | None:
        """The one card a typo clearly means, else None. Needs two or more
        words, like the one-word rule above: "platnum" alone stays a question."""
        top = self.suggest(query, limit=2)
        if top.empty or len(normalize(query).split()) < 2:
            return None
        best = top["similarity"].iloc[0]
        runner_up = top["similarity"].iloc[1] if len(top) > 1 else 0.0
        if best >= CONFIDENT_SIMILARITY and best - runner_up >= CONFIDENT_MARGIN:
            return str(top["card_id"].iloc[0])
        return None
