from datetime import UTC, date, datetime

import pytest

from card_agent.guardrails import (
    GuardrailError,
    assert_allowed_recipient,
    contains_card_number,
    sanitize_untrusted,
)
from card_agent.models import DEFAULT_VALUATIONS, BenefitKind, Category, UserProfile, WalletCard
from card_agent.store import Store


def test_luhn_detection():
    assert contains_card_number("4111 1111 1111 1111")
    assert contains_card_number("card 4012-8888-8888-1881 ok")
    assert not contains_card_number("4111 1111 1111 1112")  # fails Luhn
    assert not contains_card_number("spend $5,000 in 90 days, 75,000 points")


def test_sanitize_untrusted():
    text = "Click https://evil.example/x now! Ref 123456789012. " + "a" * 400
    clean = sanitize_untrusted(text, limit=100)
    assert "evil.example" not in clean and "123456789012" not in clean
    assert len(clean) <= 100


def test_recipient_allowlist():
    assert_allowed_recipient("Owner@Example.com", {"owner@example.com"})
    with pytest.raises(GuardrailError):
        assert_allowed_recipient("someone@else.com", {"owner@example.com"})
    with pytest.raises(GuardrailError):
        assert_allowed_recipient("owner@example.com", set())


def test_store_roundtrip(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.get_profile() == UserProfile()
    store.save_profile(UserProfile(max_annual_fee=250, home_airport="ORD"))
    store.set_spend({Category.dining: 400})
    store.set_spend({Category.gas: 80})
    store.set_valuations({"chase_ur": 1.8})
    store.set_haircuts({BenefitKind.lounge: 0.0})
    store.upsert_wallet_card(
        WalletCard(card_id="x", opened_on=date(2025, 1, 2), closed_on=date(2026, 1, 1))
    )
    store.close()

    store = Store(tmp_path / "s.db")
    assert store.get_profile().max_annual_fee == 250
    assert store.get_spend() == {Category.dining: 400, Category.gas: 80}
    valuations = store.get_valuations()
    assert valuations["chase_ur"] == 1.8 and valuations["hyatt"] == DEFAULT_VALUATIONS["hyatt"]
    assert store.get_haircuts()[BenefitKind.lounge] == 0.0
    assert store.list_wallet()[0].closed_on == date(2026, 1, 1)
    assert store.list_wallet(include_closed=False) == []
    assert store.remove_wallet_card("x") and not store.remove_wallet_card("x")
    store.mark_processed("m", "offer", datetime(2026, 9, 1, tzinfo=UTC))
    assert store.is_processed("m")


def test_store_rejects_card_numbers(tmp_path):
    store = Store(tmp_path / "s.db")
    with pytest.raises(GuardrailError):
        store.save_profile(UserProfile(home_airport="4111111111111111"))
