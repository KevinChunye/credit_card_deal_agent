import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from card_agent.collector import bonuses_api
from card_agent.collector.seed import apply_seed, load_seed
from card_agent.config import CONFIG_DIR, Settings
from card_agent.email_parse import DomainConfig, parse_message
from card_agent.inbox import poll
from card_agent.mailer import send_digest
from card_agent.models import Snapshot
from card_agent.snapshot import DataView
from card_agent.store import Store
from tests.conftest import NOW, fixture_path

OWNER = "owner@example.com"
INBOX = "kev_work@agentmail.to"


def email(name: str) -> dict:
    return json.loads(fixture_path(f"emails/{name}.json").read_text())


@pytest.fixture
def data(api_raw) -> DataView:
    cards, offers, benefits, base = bonuses_api.normalize(api_raw, NOW)
    cards, rates, protections, paths, _ = apply_seed(
        load_seed(CONFIG_DIR / "card_details.yaml"), cards, base
    )
    return DataView(Snapshot(generated_at=NOW, cards=cards, offers=offers, earn_rates=rates))


def parse(name, data):
    return parse_message(email(name), OWNER, DomainConfig.load(), data.matcher)


def test_forwarded_preapproval_from_owner(data):
    parsed = parse("forwarded_from_owner", data)
    assert parsed.accepted
    offer = parsed.offer
    assert offer.kind == "preapproval"
    assert offer.issuer == "chase"
    assert offer.sender == "no-reply@alertsp.chase.com"
    assert offer.card_id == "chase-sapphire-preferred"
    assert (offer.bonus_amount, offer.bonus_unit.value) == (75000, "points")
    assert offer.min_spend == 5000 and offer.spend_window_days == 90
    assert offer.annual_fee == 95
    assert offer.expires_at == date(2026, 12, 31)
    assert "http" not in offer.snippet and "123456789012" not in offer.snippet
    assert not offer.suspected_phishing


def test_direct_issuer_offer(data):
    offer = parse("direct_amex_offer", data).offer
    assert offer.kind == "targeted_offer"
    assert offer.issuer == "amex"
    assert offer.card_id == "amex-platinum"
    assert offer.bonus_amount == 100000
    assert offer.min_spend == 8000 and offer.spend_window_days == 180
    assert offer.expires_at == date(2026, 11, 15)
    assert offer.phishing_reasons == []


def test_lookalike_domain_rejected_as_phishing(data):
    parsed = parse("phishing_lookalike", data)
    assert not parsed.accepted
    assert "imitates chase" in parsed.reason


def test_phishing_signals_on_allowlisted_domain(data):
    parsed = parse("phishing_on_allowlisted_domain", data)
    assert parsed.accepted  # stored, but flagged and kept out of recommendations
    assert parsed.offer.suspected_phishing
    reasons = " | ".join(parsed.offer.phishing_reasons)
    assert "bit.ly" in reasons and "DKIM/DMARC" in reasons and "verify your account" in reasons


def test_non_allowlisted_sender_rejected(data):
    parsed = parse("not_allowlisted", data)
    assert not parsed.accepted and "not on the allowlist" in parsed.reason


def test_gmail_confirmation_surfaced_not_acted_on(data):
    parsed = parse("gmail_confirmation", data)
    assert parsed.accepted
    assert parsed.offer.kind == "forwarding_confirmation"
    assert parsed.offer.confirmation_code == "123456789"
    assert "http" not in parsed.offer.snippet


def test_injection_text_is_just_data(data):
    offer = parse("injection_attempt", data).offer
    assert offer.card_id == "chase-freedom-unlimited"
    assert "[link removed]" in offer.snippet
    assert "evil.example/now" not in offer.snippet


def test_owner_email_without_forward_is_rejected(data):
    message = dict(email("forwarded_from_owner"), extracted_text="just a note to self")
    parsed = parse_message(message, OWNER, DomainConfig.load(), data.matcher)
    assert not parsed.accepted


class FakeMessages:
    def __init__(self, messages: list[dict]):
        self.by_id = {m["message_id"]: m for m in messages}
        self.get_calls: list[str] = []
        self.sent: list[dict] = []

    def list(self, inbox_id, limit=None, **_):
        assert inbox_id == INBOX
        return SimpleNamespace(messages=list(self.by_id.values()))

    def get(self, inbox_id, message_id):
        self.get_calls.append(message_id)
        return self.by_id[message_id]

    def send(self, inbox_id, **kwargs):
        self.sent.append({"inbox_id": inbox_id, **kwargs})
        return SimpleNamespace(message_id="sent-1", thread_id="t")


def fake_client(messages):
    messages_api = FakeMessages(messages)
    return SimpleNamespace(inboxes=SimpleNamespace(messages=messages_api)), messages_api


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "db_path": tmp_path / "state.db",
        "agentmail_api_key": "test-key",
        "agentmail_inbox": INBOX,
        "owner_email": OWNER,
    }
    return Settings(**{**values, **overrides})


def test_poll_end_to_end_and_dedupe(tmp_path, data):
    names = [
        "forwarded_from_owner", "direct_amex_offer", "phishing_lookalike",
        "phishing_on_allowlisted_domain", "not_allowlisted", "gmail_confirmation",
        "injection_attempt", "own_sent_digest",
    ]  # fmt: skip
    client, api = fake_client([email(n) for n in names])
    store = Store(tmp_path / "state.db")
    result = poll(settings(tmp_path), store, data, NOW, client=client)

    kinds = sorted(o["kind"] for o in result["accepted"])
    assert kinds == ["forwarding_confirmation", "offer", "offer", "preapproval", "targeted_offer"]
    assert len(result["rejected"]) == 2
    assert [o["sender"] for o in result["suspected_phishing"]] == ["notify@capitalone.com"]
    assert result["action_required"][0]["confirmation_code"] == "123456789"
    assert "<sent-008@agentmail.to>" not in api.get_calls  # our own digest isn't parsed

    # Second poll: nothing re-processed, confirmation still surfaced this week.
    again = poll(settings(tmp_path), store, data, NOW + timedelta(days=1), client=client)
    assert again["accepted"] == [] and again["already_seen"] == len(names)
    assert again["action_required"]
    assert len(store.personal_offers()) == 5


def test_send_digest_only_to_owner(tmp_path):
    client, api = fake_client([])
    result = send_digest(settings(tmp_path), "Your card digest", "body", "2026-09", client=client)
    assert result["to"] == OWNER
    assert api.sent[0]["to"] == [OWNER]
    assert api.sent[0]["idempotency_key"].startswith("card-digest-2026-09-")

    routed = send_digest(
        settings(tmp_path, digest_to_email="me+cards@example.com"),
        "s",
        "b",
        "2026-09",
        client=client,
    )
    assert routed["to"] == "me+cards@example.com"

    with pytest.raises(ValueError):
        send_digest(settings(tmp_path, owner_email=None), "s", "b", "2026-09", client=client)
