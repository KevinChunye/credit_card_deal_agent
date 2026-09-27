"""Official links the agent may give you. It never applies or logs in for you:
it hands you the issuer's own page and you decide.

A card's apply link is, in order:
1. its curated issuer page (card.source_url, from config/card_sources.yaml,
   which the card-terms pipeline reads every month), else
2. the offer feed's link (card.apply_url), but only when its host is one of
   the issuer's own domains (config/issuer_domains.yaml).
Anything else gets no link: never an affiliate or referral link, never a link
from an email. Pre-qualification pages and credit resources come from
config/official_links.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

from card_agent.config import CONFIG_DIR
from card_agent.email_parse import DomainConfig
from card_agent.models import Card

LINKS_PATH = CONFIG_DIR / "official_links.yaml"


@dataclass(frozen=True)
class OfficialLinks:
    prequalify: dict[str, str]
    credit_resources: list[tuple[str, str]]  # (title, url)


@lru_cache(maxsize=1)
def load_links(path: Path = LINKS_PATH) -> OfficialLinks:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    return OfficialLinks(
        prequalify=dict(data.get("prequalify") or {}),
        credit_resources=[(r["title"], r["url"]) for r in data.get("credit_resources") or []],
    )


@lru_cache(maxsize=1)
def issuer_domains() -> DomainConfig:
    return DomainConfig.load()


def host(url: str) -> str:
    return urlparse(url).hostname or ""


def on_issuer_domain(url: str | None, issuer: str) -> bool:
    """True for an https URL on one of the issuer's own domains (or a subdomain)."""
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return issuer_domains().issuer_for(parsed.hostname) == issuer


def apply_link(card: Card) -> str | None:
    """The issuer's own page for this card, or None (discontinued, or no link
    we can vouch for)."""
    if card.discontinued:
        return None
    for url in (card.source_url, card.apply_url):
        if on_issuer_domain(url, card.issuer):
            return url
    return None


def prequalify_link(issuer: str) -> str | None:
    return load_links().prequalify.get(issuer)
