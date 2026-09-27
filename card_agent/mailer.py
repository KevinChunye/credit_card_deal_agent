"""Send the digest email via AgentMail, only ever to OWNER_EMAIL / DIGEST_TO_EMAIL."""

from __future__ import annotations

import hashlib
from typing import Any

from card_agent.config import Settings
from card_agent.guardrails import assert_allowed_recipient
from card_agent.inbox import make_client


def send_digest(
    settings: Settings,
    subject: str,
    text: str,
    period: str,
    client: Any = None,
) -> dict[str, Any]:
    recipient = settings.digest_recipient
    if not recipient:
        raise ValueError("Set OWNER_EMAIL (or DIGEST_TO_EMAIL) to email the digest.")
    assert_allowed_recipient(recipient, settings.allowed_recipients)
    client = client or make_client(settings)
    # Same month + same content = same key, so a retried run can't double-send.
    digest_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
    response = client.inboxes.messages.send(
        inbox_id=settings.agentmail_inbox,
        to=[recipient],
        subject=subject,
        text=text,
        idempotency_key=f"card-digest-{period}-{digest_hash}",
    )
    return {"to": recipient, "message_id": getattr(response, "message_id", None)}
