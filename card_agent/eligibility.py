"""Evaluate issuer eligibility rules (config/eligibility_rules.yaml) against
the wallet. Deterministic; every result says which rule fired and why."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from card_agent.config import CONFIG_DIR
from card_agent.models import Card, CardSelector, EligibilityRule, WalletCard

# Business cards from these issuers report to personal credit, so they count for 5/24.
BUSINESS_CARDS_THAT_REPORT = {"capital-one", "discover"}


@dataclass
class EligibilityResult:
    status: str = "eligible"  # eligible | ineligible | unknown
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)

    def add(self, status: str, reason: str, rule_id: str) -> None:
        rank = {"eligible": 0, "unknown": 1, "ineligible": 2}
        if rank[status] > rank[self.status]:
            self.status = status
        self.reasons.append(reason)
        self.rule_ids.append(rule_id)


def load_rules(path: Path = CONFIG_DIR / "eligibility_rules.yaml") -> list[EligibilityRule]:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    return [EligibilityRule.model_validate(rule) for rule in data.get("rules", [])]


def matches(card: Card, selector: CardSelector | dict[str, Any]) -> bool:
    if isinstance(selector, dict):
        selector = CardSelector.model_validate(selector)
    if selector.issuer and card.issuer != selector.issuer:
        return False
    if selector.name_pattern and not re.search(selector.name_pattern, card.name):
        return False
    return not (selector.card_ids and card.id not in selector.card_ids)


def months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.4375


def counts_toward_524(card_id: str, cards: dict[str, Card]) -> bool:
    card = cards.get(card_id)
    if card is None:
        return True  # unknown card: assume it counts (conservative)
    if card.counts_toward_524 is not None:
        return card.counts_toward_524
    return not card.is_business or card.issuer in BUSINESS_CARDS_THAT_REPORT


class EligibilityChecker:
    def __init__(
        self,
        rules: list[EligibilityRule],
        wallet: list[WalletCard],
        cards: dict[str, Card],
        today: date,
    ):
        self.rules = rules
        self.wallet = wallet
        self.cards = cards
        self.today = today
        # Cards you have or had, including ones you product-changed out of.
        self.history = {w.card_id for w in wallet} | {
            w.product_changed_from for w in wallet if w.product_changed_from
        }
        self.open_ids = {w.card_id for w in wallet if w.is_open}
        self.bonus_dates = {w.card_id: w.bonus_received_on for w in wallet if w.bonus_received_on}

    def recent_accounts(self, window_months: int) -> tuple[int, int]:
        """(accounts opened in the window that count for 5/24, wallet cards with no open date)."""
        counted = 0
        undated = 0
        for card in self.wallet:
            if card.product_changed_from:
                continue  # a product change is not a new account
            if card.opened_on is None:
                undated += 1
            elif months_between(card.opened_on, self.today) < window_months and counts_toward_524(
                card.card_id, self.cards
            ):
                counted += 1
        return counted, undated

    def _names(self, card_ids: list[str]) -> str:
        return ", ".join(
            self.cards[cid].display_name if cid in self.cards else cid for cid in card_ids
        )

    def _family_ids(self, family: dict[str, Any] | None, card: Card) -> set[str]:
        if not family:
            return {card.id}
        return {cid for cid, other in self.cards.items() if matches(other, family)} | {card.id}

    def _run_check(self, check: dict[str, Any], card: Card) -> tuple[str, str] | None:
        kind = check["type"]
        status = check.get("status", "ineligible")
        name = card.display_name
        if kind == "max_recent_accounts":
            window, limit = check["window_months"], check["max_accounts"]
            counted, undated = self.recent_accounts(window)
            if counted > limit:
                return (
                    status,
                    f"{counted} new personal accounts in {window} months (limit {limit + 1}/{window})",
                )
            if undated and counted + undated > limit:
                return "unknown", (
                    f"{counted} dated new accounts in {window} months, plus {undated} wallet "
                    "card(s) without an open date; add opened_on to check"
                )
            return None
        if kind == "once_per_lifetime":
            if card.id in self.history or card.id in self.bonus_dates:
                return status, f"you have or had the {name}"
            return None
        if kind == "blocked_by_prior":
            prior = sorted(set(check["cards"]) & self.history)
            if prior:
                return status, f"you have or had {self._names(prior)}"
            return None
        if kind == "bonus_window":
            window = check["window_months"]
            family = (
                {card.id}
                if check.get("scope") == "same_card"
                else self._family_ids(check.get("family"), card)
            )
            for cid in sorted(family):
                received = self.bonus_dates.get(cid)
                if received and months_between(received, self.today) < window:
                    return (
                        status,
                        f"bonus on {self._names([cid])} received {received.isoformat()} "
                        f"(within {window} months)",
                    )
            return None
        if kind == "holding_blocks":
            family = (
                {card.id}
                if check.get("scope") == "same_card"
                else self._family_ids(check.get("family"), card)
            )
            held = sorted(family & self.open_ids)
            if held:
                return status, f"you currently hold {self._names(held)}"
            return None
        raise ValueError(f"Unknown eligibility check type {kind!r}")

    def check(self, card: Card) -> EligibilityResult:
        result = EligibilityResult()
        for rule in self.rules:
            if not matches(card, rule.applies_to):
                continue
            for check in rule.checks:
                outcome = self._run_check(check, card)
                if outcome:
                    status, reason = outcome
                    result.add(status, f"{rule.id}: {reason}", rule.id)
        return result
