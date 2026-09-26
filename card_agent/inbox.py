"""`inbox poll`: read new messages from the AgentMail inbox, keep issuer offers.

Read-only toward AgentMail: we list and get messages, and track what we've
seen in SQLite. We never label, reply to, forward, or click anything in a
message. Uses the official `agentmail` SDK (AgentMail(api_key=...),
client.inboxes.messages.list/get).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from card_agent.config import Settings
from card_agent.email_parse import DomainConfig, parse_message
from card_agent.snapshot import DataView
from card_agent.store import Store


class InboxNotConfigured(RuntimeError):
    pass


def make_client(settings: Settings):
    if not settings.agentmail_api_key or not settings.agentmail_inbox:
        raise InboxNotConfigured("Set AGENTMAIL_API_KEY and AGENTMAIL_INBOX to use the inbox.")
    from agentmail import AgentMail

    return AgentMail(api_key=settings.agentmail_api_key)


def _as_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    return message.model_dump()


def poll(
    settings: Settings,
    store: Store,
    data: DataView,
    now: datetime,
    client: Any = None,
    limit: int = 50,
) -> dict[str, Any]:
    client = client or make_client(settings)
    inbox_id = settings.agentmail_inbox
    config = DomainConfig.load()
    listing = client.inboxes.messages.list(inbox_id=inbox_id, limit=limit)

    accepted: list[dict] = []
    rejected: list[dict] = []
    skipped = 0
    for item in listing.messages:
        summary = _as_dict(item)
        message_id = summary["message_id"]
        sender = (summary.get("from_") or summary.get("from") or "").lower()
        if store.is_processed(message_id):
            skipped += 1
            continue
        if "sent" in (summary.get("labels") or []) or (inbox_id and inbox_id.lower() in sender):
            store.mark_processed(message_id, "own outgoing message", now)
            continue
        full = _as_dict(client.inboxes.messages.get(inbox_id=inbox_id, message_id=message_id))
        parsed = parse_message(full, settings.owner_email, config, data.matcher)
        if parsed.accepted and parsed.offer:
            store.save_personal_offer(parsed.offer)
            store.mark_processed(message_id, parsed.offer.kind, now)
            accepted.append(parsed.offer.model_dump(mode="json"))
        else:
            store.mark_processed(message_id, f"rejected: {parsed.reason}", now)
            rejected.append({"message_id": message_id, "reason": parsed.reason})

    # Keep surfacing a Gmail forwarding confirmation for two weeks, even if an
    # earlier (e.g. scheduled) poll is what first saw it.
    confirmations = [
        offer
        for offer in store.personal_offers(since=now - timedelta(days=14))
        if offer.kind == "forwarding_confirmation"
    ]
    action_required = [
        {
            "type": "gmail_forwarding_confirmation",
            "confirmation_code": offer.confirmation_code,
            "instructions": (
                "Gmail wants to confirm forwarding to this inbox. In Gmail: Settings > "
                "Forwarding and POP/IMAP > enter this confirmation code (or open the email "
                "in the AgentMail console and click the link yourself). The agent will not "
                "click it for you."
            ),
            "received_at": offer.received_at.isoformat(),
        }
        for offer in confirmations
    ]
    phishing = [offer for offer in accepted if offer["suspected_phishing"]]
    return {
        "checked": len(listing.messages),
        "already_seen": skipped,
        "accepted": accepted,
        "rejected": rejected,
        "suspected_phishing": phishing,
        "action_required": action_required,
    }
