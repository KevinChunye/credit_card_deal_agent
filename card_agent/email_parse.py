"""Deterministic extraction from issuer offer emails. No LLM.

Every email is untrusted data: we never follow its instructions, never fetch
its links, and never pass it to anything with side effects. We read the sender
domain, regex the subject/body for the offer terms, keep a sanitized snippet,
and flag anything that looks like phishing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import yaml

from card_agent.config import CONFIG_DIR
from card_agent.guardrails import sanitize_untrusted
from card_agent.matching import ISSUER_ALIASES, CardMatcher, issuers_in
from card_agent.models import ISSUER_DISPLAY, BonusUnit, PersonalOffer

FORWARD_HEADER = re.compile(
    r"-{2,}\s*Forwarded message\s*-{2,}.*?From:\s*(?P<from>[^\n]+)", re.I | re.S
)
BONUS = re.compile(
    r"(?P<pts>\d{1,3}(?:,\d{3})+|\d{2,3}k)\s+(?:bonus\s+)?(?:[\w®]+\s+){0,3}?(?P<unit>points|miles)"
    r"|\$(?P<usd>\d{1,3}(?:,\d{3})*)\s+(?:cash\s+|welcome\s+|statement\s+)?(?:bonus|cash back|credit|back)",
    re.I,
)
MIN_SPEND = re.compile(
    r"(?:spend|spending|purchases? (?:of|totaling)|make)\s+(?:at least\s+)?\$(?P<amount>\d{1,3}(?:,\d{3})*)",
    re.I,
)
WINDOW = re.compile(
    r"(?:in|within)\s+(?:the\s+|your\s+)?(?:first\s+)?(?P<n>\d{1,3})\s+(?P<unit>days?|months?)",
    re.I,
)
ANNUAL_FEE = re.compile(
    r"\$(?P<a>\d{1,3})\s+annual\s+fee|annual\s+fee\s+(?:of\s+|is\s+)?\$(?P<b>\d{1,3})|\b(?P<none>no|\$0)\s+annual\s+fee",
    re.I,
)
EXPIRES = re.compile(
    r"(?:expires?|offer ends|respond by|apply by|valid through|valid until)\s*(?:on\s+)?:?\s*"
    r"(?P<date>[A-Z][a-z]+\.? \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)
PREAPPROVAL = re.compile(r"pre-?approved|pre-?selected|pre-?qualified", re.I)
TARGETED = re.compile(
    r"\btargeted\b|exclusive offer|just for you|upgrade offer|special offer|spend offer", re.I
)
SUSPICIOUS_PHRASES = [
    (re.compile(r"verify your (account|identity)", re.I), "asks you to verify your account"),
    (re.compile(r"(account|card) (has been |is )?(suspended|locked|limited|on hold)", re.I), "claims the account is locked or suspended"),
    (re.compile(r"(confirm|update|enter) your (password|pin|ssn|social security|card number|login)", re.I), "asks for a password, PIN, SSN or card number"),
    (re.compile(r"unusual (sign-?in|activity)", re.I), "unusual-activity scare language"),
    (re.compile(r"within 24 hours|immediately or", re.I), "urgency pressure"),
    (re.compile(r"gift card", re.I), "mentions gift cards"),
]  # fmt: skip
HREF = re.compile(r"""href=["']https?://([^/"'\s:]+)""", re.I)


@dataclass
class DomainConfig:
    issuers: dict[str, list[str]]
    gmail_sender: str

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "issuer_domains.yaml") -> DomainConfig:
        with path.open() as handle:
            data = yaml.safe_load(handle) or {}
        return cls(
            issuers={
                issuer: [d.lower() for d in domains] for issuer, domains in data["issuers"].items()
            },
            gmail_sender=data.get(
                "gmail_forwarding_confirmation_sender", "forwarding-noreply@google.com"
            ),
        )

    def issuer_for(self, domain: str) -> str | None:
        domain = domain.lower().rstrip(".")
        for issuer, domains in self.issuers.items():
            if any(domain == d or domain.endswith("." + d) for d in domains):
                return issuer
        return None


def domain_of(address: str) -> str:
    return parseaddr(address)[1].rpartition("@")[2].lower()


@dataclass
class ParsedEmail:
    accepted: bool
    reason: str
    offer: PersonalOffer | None = None
    phishing_reasons: list[str] = field(default_factory=list)


def _parse_date(raw: str) -> date | None:
    raw = raw.replace(".", "").replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def extract_terms(text: str) -> dict[str, Any]:
    terms: dict[str, Any] = {}
    bonus = BONUS.search(text)
    if bonus:
        if bonus.group("pts"):
            raw = bonus.group("pts").lower()
            terms["bonus_amount"] = (
                float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw.replace(",", ""))
            )
            terms["bonus_unit"] = BonusUnit(bonus.group("unit").lower())
        else:
            terms["bonus_amount"] = float(bonus.group("usd").replace(",", ""))
            terms["bonus_unit"] = BonusUnit.usd
    spend = MIN_SPEND.search(text)
    if spend:
        terms["min_spend"] = float(spend.group("amount").replace(",", ""))
        window = WINDOW.search(text, spend.end(), spend.end() + 120)
        if window:
            n = int(window.group("n"))
            terms["spend_window_days"] = (
                n * 30 if window.group("unit").lower().startswith("month") else n
            )
    fee = ANNUAL_FEE.search(text)
    if fee:
        terms["annual_fee"] = 0.0 if fee.group("none") else float(fee.group("a") or fee.group("b"))
    expires = EXPIRES.search(text)
    if expires:
        terms["expires_at"] = _parse_date(expires.group("date"))
    return terms


def phishing_signals(
    sender: str,
    display_name: str,
    subject: str,
    body: str,
    html: str | None,
    headers: dict[str, str] | None,
    config: DomainConfig,
) -> list[str]:
    reasons: list[str] = []
    sender_domain = domain_of(sender)
    sender_issuer = config.issuer_for(sender_domain)

    # Display name claims an issuer the domain doesn't belong to.
    claimed = issuers_in(display_name) if display_name else set()
    if claimed and sender_issuer not in claimed:
        reasons.append(
            f"display name claims {', '.join(sorted(claimed))} but sender domain is {sender_domain}"
        )

    # Links pointing somewhere other than an issuer domain (parsed, never fetched).
    for link_domain in sorted(set(HREF.findall(html or ""))):
        if config.issuer_for(link_domain) is None:
            reasons.append(f"link points to non-issuer domain {link_domain}")
            break

    text = f"{subject}\n{body}"
    reasons += [label for pattern, label in SUSPICIOUS_PHRASES if pattern.search(text)]

    auth = " ".join(
        v for k, v in (headers or {}).items() if k.lower() == "authentication-results"
    ).lower()
    if re.search(r"\b(dkim|dmarc)=fail", auth):
        reasons.append("DKIM/DMARC authentication failed")
    return reasons


def lookalike_issuer(domain: str, config: DomainConfig) -> str | None:
    """An issuer name inside a domain that is not that issuer's (e.g. chase-alerts.com)."""
    if config.issuer_for(domain):
        return None
    flat = domain.replace("-", " ").replace(".", " ")
    for issuer, aliases in ISSUER_ALIASES.items():
        names = [a for a in aliases if len(a) > 3] + [
            d.split(".")[0] for d in config.issuers.get(issuer, [])
        ]
        if any(
            re.search(rf"\b{re.escape(name)}", flat) or name.replace(" ", "") in domain
            for name in names
        ):
            return issuer
    return None


def parse_message(
    message: dict[str, Any],
    owner_email: str | None,
    config: DomainConfig,
    matcher: CardMatcher,
) -> ParsedEmail:
    """Turn one AgentMail message (as a dict) into a PersonalOffer or a rejection."""
    raw_from = message.get("from_") or message.get("from") or ""
    display_name, sender = parseaddr(raw_from)
    sender = sender.lower()
    subject = message.get("subject") or ""
    body = message.get("extracted_text") or message.get("text") or message.get("preview") or ""
    html = message.get("html")
    headers = message.get("headers") or {}
    received = message.get("timestamp") or message.get("created_at") or datetime.now().astimezone()
    if isinstance(received, str):
        received = datetime.fromisoformat(received.replace("Z", "+00:00"))
    message_id = message["message_id"]

    # Gmail forwarding confirmation: surface it, never act on it.
    if sender == config.gmail_sender:
        code = re.search(r"\(#(\d{5,12})\)", subject) or re.search(
            r"confirmation code:\s*(\d{5,12})", body, re.I
        )
        offer = PersonalOffer(
            message_id=message_id,
            received_at=received,
            kind="forwarding_confirmation",
            sender=sender,
            subject=sanitize_untrusted(subject, 160),
            snippet="Gmail is asking to confirm forwarding to this inbox.",
            confirmation_code=code.group(1) if code else None,
        )
        return ParsedEmail(True, "gmail forwarding confirmation (surfaced, not acted on)", offer)

    # Manual forward from you: the original sender must itself be allowlisted.
    original_sender, original_name = sender, display_name
    forwarded = False
    if owner_email and sender == owner_email.lower():
        header = FORWARD_HEADER.search(body)
        if not header:
            return ParsedEmail(False, "from OWNER_EMAIL but no forwarded issuer email found")
        original_name, original_sender = parseaddr(header.group("from").strip())
        original_sender = original_sender.lower()
        forwarded = True

    domain = domain_of(original_sender)
    issuer = config.issuer_for(domain)
    if issuer is None:
        lookalike = lookalike_issuer(domain, config)
        if lookalike:
            reason = f"sender domain {domain} imitates {lookalike}; suspected phishing, not stored"
            return ParsedEmail(False, reason, phishing_reasons=[reason])
        return ParsedEmail(False, f"sender {original_sender or '(none)'} is not on the allowlist")

    reasons = phishing_signals(
        original_sender,
        original_name,
        subject,
        body,
        html,
        None if forwarded else headers,
        config,
    )
    text = f"{subject}\n{body}"
    terms = extract_terms(text)
    # The sender domain already tells us the issuer, so "the Platinum Card" is enough.
    issuer_name = ISSUER_DISPLAY.get(issuer, issuer)
    card_ids = [
        cid for cid in matcher.find(f"{issuer_name} {text}") if matcher.cards[cid].issuer == issuer
    ]
    if PREAPPROVAL.search(text):
        kind = "preapproval"
    elif TARGETED.search(text):
        kind = "targeted_offer"
    elif terms.get("bonus_amount"):
        kind = "offer"
    else:
        kind = "other"
    offer = PersonalOffer(
        message_id=message_id,
        received_at=received,
        kind=kind,
        sender=original_sender,
        issuer=issuer,
        card_id=card_ids[0] if card_ids else None,
        subject=sanitize_untrusted(subject, 160),
        snippet=sanitize_untrusted(body, 280),
        suspected_phishing=len(reasons) >= 2,
        phishing_reasons=reasons,
        **terms,
    )
    return ParsedEmail(True, "accepted", offer, reasons)
