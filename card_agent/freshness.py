"""Staleness guard: how current is each card's terms data?

The collector stamps every card with the card-terms pipeline's source_status
(ok / fetch_failed / validation_failed / manual) and last_verified date.
`rank`, `compare`, `explain` and the digest put a warning marker on a card
whose terms aren't "ok" or were last verified against the issuer's page more
than 60 days ago. Deterministic; no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from card_agent.models import Card
from card_agent.terms.state import STALE_AFTER_DAYS

MARKER = "⚠"
THIS_MONTH_DAYS = 31


def terms_warning(card: Card, today: date) -> str | None:
    """Why this card's terms may be out of date, or None if recently verified."""
    if not card.terms_tracked:
        return "terms not checked against an issuer page"
    verified = card.last_verified
    if card.source_status == "ok" and verified and (today - verified).days <= STALE_AFTER_DAYS:
        return None
    since = f"last verified {verified:%Y-%m-%d}" if verified else "never verified"
    if card.source_status == "manual":
        if card.source_url is None:
            return "terms hand-maintained (no readable issuer page)"
        return f"terms {since} against the issuer page"
    if card.source_status == "fetch_failed":
        return f"issuer page unreachable; terms {since}"
    if card.source_status == "validation_failed":
        return f"issuer page didn't pass validation; terms {since}"
    return f"terms {since} (over {STALE_AFTER_DAYS} days ago)"


def flag(card: Card, today: date) -> str:
    """The marker and the reason, or an empty string when recently verified."""
    warning = terms_warning(card, today)
    return f"{MARKER} {warning}" if warning else ""


def verified_label(card: Card, today: date) -> str:
    """Short table cell: the verification date, with the marker when stale."""
    shown = f"{card.last_verified:%Y-%m-%d}" if card.last_verified else "never"
    return shown if terms_warning(card, today) is None else f"{MARKER} {shown}"


@dataclass
class DataHealth:
    tracked: int = 0
    verified_this_month: int = 0
    stale: list[tuple[str, str]] = field(default_factory=list)  # (card name, reason)

    @property
    def line(self) -> str:
        verified = self.verified_this_month
        return (
            f"Data health: {verified} card{'' if verified == 1 else 's'} verified this month, "
            f"{len(self.stale)} stale."
        )


def data_health(cards: list[Card], today: date) -> DataHealth:
    """Counts over the cards the terms pipeline tracks. "Verified this month" is
    verified in the last 31 days and not stale."""
    health = DataHealth()
    for card in cards:
        if not card.terms_tracked:
            continue
        health.tracked += 1
        warning = terms_warning(card, today)
        if warning:
            health.stale.append((card.display_name, warning))
        elif (today - card.last_verified).days <= THIS_MONTH_DAYS:
            health.verified_this_month += 1
    return health
