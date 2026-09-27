"""Set up profile, spend, valuations, haircuts and wallet: from YAML, from a
JSON patch (what the agent uses after asking you in chat), or interactively."""

from __future__ import annotations

from datetime import date
from typing import Any

from card_agent.guardrails import reject_card_numbers
from card_agent.matching import CardMatcher
from card_agent.models import BenefitKind, Category, UserProfile, WalletCard
from card_agent.store import Store


def resolve_card_id(query: str, matcher: CardMatcher | None) -> str:
    if matcher is None:
        return query  # no snapshot yet: store as given
    card_id, candidates = matcher.resolve(query)
    if card_id:
        return card_id
    if candidates:
        raise ValueError(f"{query!r} is ambiguous: {', '.join(candidates[:8])}")
    raise ValueError(f"No card matches {query!r}. Try the id from `rank --json`.")


def apply_patch(store: Store, patch: dict[str, Any], matcher: CardMatcher | None) -> dict[str, Any]:
    """Merge a partial setup into the store. Returns what changed."""
    reject_card_numbers(str(patch))
    unknown = set(patch) - {"profile", "monthly_spend", "valuations", "haircuts", "wallet"}
    if unknown:
        raise ValueError(f"Unknown section(s): {', '.join(sorted(unknown))}")
    changed: dict[str, Any] = {}
    if "profile" in patch:
        current = store.get_profile().model_dump()
        updates = dict(patch["profile"] or {})
        if "goal_weights" in updates:
            updates["goal_weights"] = {**current["goal_weights"], **updates["goal_weights"]}
        profile = UserProfile.model_validate({**current, **updates})
        store.save_profile(profile)
        changed["profile"] = sorted(updates)
    elif not store.has_profile():
        store.save_profile(UserProfile())
    if "monthly_spend" in patch:
        spend = {Category(k): float(v) for k, v in (patch["monthly_spend"] or {}).items()}
        store.set_spend(spend)
        changed["monthly_spend"] = sorted(c.value for c in spend)
    if "valuations" in patch:
        valuations = {k: float(v) for k, v in (patch["valuations"] or {}).items()}
        if any(v <= 0 for v in valuations.values()):
            raise ValueError("Valuations must be positive cents per point.")
        store.set_valuations(valuations)
        changed["valuations"] = sorted(valuations)
    if "haircuts" in patch:
        haircuts = {BenefitKind(k): float(v) for k, v in (patch["haircuts"] or {}).items()}
        if any(not 0 <= v <= 1 for v in haircuts.values()):
            raise ValueError("Haircuts are the share of a benefit you use: 0.0 to 1.0.")
        store.set_haircuts(haircuts)
        changed["haircuts"] = sorted(k.value for k in haircuts)
    if "wallet" in patch:
        added = []
        for entry in patch["wallet"] or []:
            entry = dict(entry)
            entry["card_id"] = resolve_card_id(str(entry["card_id"]), matcher)
            if entry.get("product_changed_from"):
                entry["product_changed_from"] = resolve_card_id(
                    str(entry["product_changed_from"]), matcher
                )
            store.upsert_wallet_card(WalletCard.model_validate(entry))
            added.append(entry["card_id"])
        changed["wallet"] = added
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
