"""Set up profile, spend, valuations, haircuts and wallet: from YAML, from a
JSON patch (what the agent uses after asking you in chat), or interactively."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from card_agent.guardrails import reject_card_numbers
from card_agent.matching import CardMatcher
from card_agent.models import BenefitKind, Category, GoalWeights, UserProfile, WalletCard
from card_agent.store import Store


def resolve_card_id(query: str, matcher: CardMatcher | None) -> str:
    if matcher is None:
        return query  # no snapshot yet: store as given
    card_id, candidates = matcher.resolve(query)
    if card_id:
        return card_id
    if candidates:
        names = "; ".join(matcher.cards[c].display_name for c in candidates[:6])
        raise ValueError(f"{query!r} could be several cards: {names}. Which one do you mean?")
    raise ValueError(
        f"I don't know a card called {query!r}. Could you give its full name, like "
        '"Chase Sapphire Preferred"?'
    )


def _section(patch: dict[str, Any], name: str) -> dict[str, Any]:
    value = patch.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object of name: value pairs.")
    return value


def _number(key: str, value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key!r}: {value!r} isn't a number ({label}).") from None
    if not math.isfinite(number):
        raise ValueError(f"{key!r}: {value!r} isn't a usable number ({label}).")
    return number


def _categories(values: dict[str, Any], enum, label: str) -> dict:
    parsed = {}
    for key, value in values.items():
        try:
            member = enum(key)
        except ValueError:
            options = ", ".join(member.value for member in enum)
            raise ValueError(f"{key!r} isn't a valid {label}. Use: {options}.") from None
        parsed[member] = _number(key, value, label)
    return parsed


def _known_keys(given: dict[str, Any], model, what: str) -> None:
    unknown = sorted(set(given) - set(model.model_fields))
    if unknown:
        valid = ", ".join(model.model_fields)
        raise ValueError(f"Unknown {what} field(s): {', '.join(unknown)}. Valid: {valid}.")


def apply_patch(store: Store, patch: dict[str, Any], matcher: CardMatcher | None) -> dict[str, Any]:
    """Merge a partial setup into the store. Returns what changed.

    Everything is validated before anything is written, and the read, checks
    and writes happen under one write lock: a bad value anywhere saves
    nothing, and two updates at once can't overwrite each other.
    """
    if not isinstance(patch, dict):
        raise ValueError('The setup must be a JSON object, like {"monthly_spend": {...}}.')
    reject_card_numbers(str(patch))
    unknown = set(patch) - {"profile", "monthly_spend", "valuations", "haircuts", "wallet"}
    if unknown:
        raise ValueError(f"Unknown section(s): {', '.join(sorted(unknown))}")
    entries = patch.get("wallet") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError('wallet must be a list of cards, like [{"card_id": "amex gold"}].')

    changed: dict[str, Any] = {}
    with store.transaction(immediate=True):
        # 1. Validate everything.
        profile = updates = None
        if "profile" in patch:
            updates = _section(patch, "profile")
            _known_keys(updates, UserProfile, "profile")
            current = store.get_profile().model_dump()
            if "goal_weights" in updates:
                weights = updates["goal_weights"]
                if not isinstance(weights, dict):
                    raise ValueError('goal_weights must be an object, like {"travel": 0.6}.')
                _known_keys(weights, GoalWeights, "goal_weights")
                updates = {**updates, "goal_weights": {**current["goal_weights"], **weights}}
            profile = UserProfile.model_validate({**current, **updates})
        spend = _categories(_section(patch, "monthly_spend"), Category, "spend category")
        if any(v < 0 for v in spend.values()):
            raise ValueError("Monthly spend can't be negative.")
        valuations = {
            k: _number(k, v, "cents per point") for k, v in _section(patch, "valuations").items()
        }
        if any(v <= 0 for v in valuations.values()):
            raise ValueError("Valuations must be positive cents per point.")
        haircuts = _categories(_section(patch, "haircuts"), BenefitKind, "benefit kind")
        if any(not 0 <= v <= 1 for v in haircuts.values()):
            raise ValueError("Haircuts are the share of a benefit you use: 0.0 to 1.0.")
        wallet = []
        for entry in entries:
            entry = dict(entry)
            _known_keys(entry, WalletCard, "wallet")
            if not entry.get("card_id"):
                raise ValueError("Each wallet entry needs a card_id (the card's name works too).")
            entry["card_id"] = resolve_card_id(str(entry["card_id"]), matcher)
            if entry.get("product_changed_from"):
                entry["product_changed_from"] = resolve_card_id(
                    str(entry["product_changed_from"]), matcher
                )
            wallet.append(WalletCard.model_validate(entry))

        # 2. Write it all (or, on any error, nothing).
        if profile is not None:
            store.save_profile(profile)
            changed["profile"] = sorted(updates)
        elif not store.has_profile():
            store.save_profile(UserProfile())
        if "monthly_spend" in patch:
            store.set_spend(spend)
            changed["monthly_spend"] = sorted(c.value for c in spend)
        if "valuations" in patch:
            store.set_valuations(valuations)
            changed["valuations"] = sorted(valuations)
        if "haircuts" in patch:
            store.set_haircuts(haircuts)
            changed["haircuts"] = sorted(k.value for k in haircuts)
        if "wallet" in patch:
            for card in wallet:
                store.upsert_wallet_card(card)
            changed["wallet"] = [card.card_id for card in wallet]
    return changed


def show(store: Store) -> dict[str, Any]:
    return {
        "profile": store.get_profile().model_dump(mode="json"),
        "monthly_spend": {k.value: v for k, v in store.get_spend().items()},
        "valuations": store.get_valuations(),
        "haircuts": {k.value: v for k, v in store.get_haircuts().items()},
        "wallet": [w.model_dump(mode="json") for w in store.list_wallet()],
    }


def _ask(prompt: str, default: Any = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or ("" if default is None else str(default))


def interactive(store: Store, matcher: CardMatcher | None) -> dict[str, Any]:
    """A short terminal walkthrough; skip any question with Enter."""
    profile = store.get_profile()
    print("Setting up your card profile. Press Enter to keep the value in brackets.")
    print("Never type card numbers or passwords here.\n")
    patch: dict[str, Any] = {"profile": {}, "monthly_spend": {}, "wallet": []}
    goal = _ask(
        "Main goal (travel / cash_back / business / credit_building / bonus_churning)", "travel"
    )
    if goal in profile.goal_weights.model_dump():
        patch["profile"]["goal_weights"] = {
            g: (0.7 if g == goal else 0.3 / 4) for g in profile.goal_weights.model_dump()
        }
    patch["profile"]["max_annual_fee"] = float(
        _ask("Max annual fee you'd pay", profile.max_annual_fee)
    )
    patch["profile"]["trips_per_year"] = int(_ask("Trips per year", profile.trips_per_year))
    patch["profile"]["has_business"] = (
        _ask("Do you have a business (y/n)", "n").lower().startswith("y")
    )
    print("\nMonthly spend in USD (Enter to skip a category):")
    current = store.get_spend()
    for category in Category:
        answer = _ask(f"  {category.value}", current.get(category, ""))
        if answer:
            patch["monthly_spend"][category.value] = float(answer)
    print("\nCards you hold, one per line (e.g. 'sapphire preferred'); blank line to finish:")
    while True:
        answer = _ask("  card")
        if not answer:
            break
        opened = _ask("    opened on (YYYY-MM-DD, optional)")
        entry: dict[str, Any] = {"card_id": answer}
        if opened:
            entry["opened_on"] = date.fromisoformat(opened)
        patch["wallet"].append(entry)
    return apply_patch(store, patch, matcher)
