"""Hard guardrails, enforced in code rather than left to the prompt.

- The agent never applies for cards and never logs into any account: there is
  no code path that does either, and none should be added.
- No card numbers or bank credentials are ever stored.
- Email goes only to OWNER_EMAIL / DIGEST_TO_EMAIL.
- Email and scraped text is untrusted data: links are stripped and long digit
  runs redacted before anything is stored or shown.
"""

from __future__ import annotations

import re


class GuardrailError(ValueError):
    """Raised when an action would break a hard rule."""


_DIGIT_RUN = re.compile(r"(?:\d[ -]?){13,19}")
_LONG_NUMBER = re.compile(r"\b\d{9,}\b")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def contains_card_number(text: str) -> bool:
    for match in _DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return True
    return False


def reject_card_numbers(text: str) -> None:
    if contains_card_number(text):
        raise GuardrailError(
            "That looks like a card number. This agent never stores card numbers; "
            "remove it and try again."
        )


def assert_allowed_recipient(address: str, allowed: set[str]) -> None:
    if not allowed:
        raise GuardrailError("OWNER_EMAIL is not set; refusing to send email.")
    if address.strip().lower() not in allowed:
        raise GuardrailError(
            f"Refusing to email {address!r}: only OWNER_EMAIL / DIGEST_TO_EMAIL are allowed."
        )


def sanitize_untrusted(text: str, limit: int = 280) -> str:
    """Make email/scraped text safe to store and relay: no links, no long
    numbers (account/reference numbers), no control characters, bounded length."""
    text = _URL.sub("[link removed]", text or "")
    text = _DIGIT_RUN.sub("[number removed]", text)
    text = _LONG_NUMBER.sub("[number removed]", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
