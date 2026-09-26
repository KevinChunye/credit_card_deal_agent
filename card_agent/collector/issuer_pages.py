"""Easy-mode issuer page reading: one plain GET, then look for facts in the
static HTML, JSON-LD, __NEXT_DATA__, or inline state. No JavaScript execution.

Used two ways:
- scripts/probe_issuers.py reports which pages are readable this way.
- The collector fetches only pages marked `collect: true` in
  config/issuer_pages.yaml and cross-checks the bonuses API against them.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from card_agent.collector.http import HostThrottle, RobotsCache

BLOCKED_STATUS = {401, 403, 405, 429, 503}
BLOCK_MARKERS = (
    "access denied",
    "pardon our interruption",
    "request unsuccessful",
    "_incapsula_resource",
    "px-captcha",
    "cf-chl",
    "just a moment...",
    "attention required! | cloudflare",
    "bm-verify",
    "are you a robot",
    "unusual traffic",
)
JS_MARKERS = (
    "enable javascript",
    "requires javascript",
    "javascript is disabled",
    "you need to enable javascript",
)

_NUM = r"\d{1,3}(?:,\d{3})+|\d+"
BONUS_PATTERNS = (
    re.compile(r"(\d{1,3}(?:,\d{3})+)\s+(?:bonus\s+|welcome\s+|total\s+)?(points|miles)", re.I),
    re.compile(r"(\d{2,3})k\s+(?:bonus\s+)?(points|miles)", re.I),
    re.compile(
        r"\$(\d{1,3}(?:,\d{3})*)\s+(?:cash\s+|welcome\s+|bonus\s+)?"
        r"(?:rewards?\s+)?(bonus|cash back|statement credit|cash)\b",
        re.I,
    ),
)
ANNUAL_FEE_PATTERNS = (
    re.compile(r"\$(\d{1,3}(?:,\d{3})?)\s+annual\s+fee", re.I),
    re.compile(
        r"annual\s+(?:membership\s+)?fee(?:\s+(?:is|of))?\s*:?\s*\$(\d{1,3}(?:,\d{3})?)", re.I
    ),
    re.compile(r"\b(no|\$0)\s+annual\s+fee", re.I),
)
EARN_PATTERNS = (
    re.compile(
        r"(\d{1,2}(?:\.\d{1,2})?)\s?x\s+(?:total\s+)?(?:points|miles)?\s*(?:on|at|for)\s+"
        r"([a-z][a-z ,&'/-]{2,50})",
        re.I,
    ),
    re.compile(
        r"(\d{1,2}(?:\.\d{1,2})?)%\s+(?:unlimited\s+)?(?:cash\s+back|cash\s+rewards?)\s+(?:on|at|for)\s+"
        r"([a-z][a-z ,&'/-]{2,50})",
        re.I,
    ),
    re.compile(
        r"(\d{1,2}(?:\.\d{1,2})?)\s+(?:points|miles)\s+per\s+(?:\$1|dollar)\s+(?:spent\s+)?(?:on|at)\s+"
        r"([a-z][a-z ,&'/-]{2,50})",
        re.I,
    ),
)


def _to_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def html_to_text(page: str) -> str:
    """Visible text only: drop scripts, styles, and tags."""
    without_code = re.sub(
        r"<(script|style|noscript|template)\b.*?</\1>", " ", page, flags=re.S | re.I
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", html_lib.unescape(without_tags)).strip()


def json_ld_blocks(page: str) -> list[Any]:
    blocks = []
    pattern = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    for raw in re.findall(pattern, page, flags=re.S | re.I):
        try:
            blocks.append(json.loads(raw.strip()))
        except json.JSONDecodeError:
            continue
    return blocks


def next_data(page: str) -> Any | None:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, flags=re.S | re.I
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def inline_state_chunks(page: str) -> list[str]:
    """Raw text of `window.__SOMETHING__ = {...}` style state blobs."""
    pattern = r"window\.(__[A-Z0-9_]+__)\s*=\s*(.{20,}?)</script>"
    return [chunk for _name, chunk in re.findall(pattern, page, flags=re.S)]


def flatten_strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for value in obj.values() for s in flatten_strings(value)]
    if isinstance(obj, list):
        return [s for value in obj for s in flatten_strings(value)]
    return []


def extract_facts(text: str) -> dict[str, list]:
    """Regex the bonus, annual fee and earn rates out of a block of text."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    bonuses = []
    for pattern in BONUS_PATTERNS:
        for match in pattern.finditer(text):
            amount = _to_number(match.group(1))
            unit = match.group(2).lower()
            if unit in ("points", "miles") and pattern is BONUS_PATTERNS[1]:
                amount *= 1000
            if unit not in ("points", "miles"):
                unit = "usd"
            if (unit == "usd" and amount < 50) or (unit != "usd" and amount < 1000):
                continue
            bonuses.append({"amount": amount, "unit": unit, "evidence": _evidence(text, match)})
    fees = []
    for pattern in ANNUAL_FEE_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1)
            amount = 0.0 if raw.lower() in ("no", "$0") else _to_number(raw)
            fees.append({"amount": amount, "evidence": _evidence(text, match)})
    earn = []
    for pattern in EARN_PATTERNS:
        for match in pattern.finditer(text):
            earn.append(
                {
                    "rate": float(match.group(1)),
                    "on": match.group(2).strip(" ,&-").lower(),
                    "evidence": _evidence(text, match),
                }
            )
    return {"bonus": bonuses[:5], "annual_fee": fees[:5], "earn_rates": earn[:10]}


def _evidence(text: str, match: re.Match, width: int = 60) -> str:
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return text[start:end].strip()


@dataclass
class PageAnalysis:
    status_code: int | None
    bytes: int = 0
    blocked: bool = False
    js_only: bool = False
    methods: dict[str, dict[str, list]] = field(default_factory=dict)
    note: str | None = None

    @property
    def fields_available(self) -> list[str]:
        found = {name for facts in self.methods.values() for name, items in facts.items() if items}
        return [name for name in ("bonus", "annual_fee", "earn_rates") if name in found]

    @property
    def working_methods(self) -> list[str]:
        return [name for name, facts in self.methods.items() if any(facts.values())]

    def best(self, field_name: str) -> dict | None:
        """First hit for a field, preferring structured sources over page text."""
        for method in ("json_ld", "next_data", "inline_state", "static_text"):
            items = self.methods.get(method, {}).get(field_name) or []
            if items:
                return items[0]
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "bytes": self.bytes,
            "blocked": self.blocked,
            "js_only": self.js_only,
            "working_methods": self.working_methods,
            "fields_available": self.fields_available,
            "note": self.note,
            "methods": self.methods,
        }


def analyze_page(status_code: int | None, page: str) -> PageAnalysis:
    analysis = PageAnalysis(status_code=status_code, bytes=len(page.encode("utf-8", "ignore")))
    lowered = page[:200_000].lower()
    if status_code in BLOCKED_STATUS:
        analysis.blocked = True
        analysis.note = f"HTTP {status_code}"
        return analysis

    analysis.methods["json_ld"] = extract_facts(" ".join(flatten_strings(json_ld_blocks(page))))
    data = next_data(page)
    analysis.methods["next_data"] = extract_facts(" ".join(flatten_strings(data)) if data else "")
    analysis.methods["inline_state"] = extract_facts(" ".join(inline_state_chunks(page)))
    text = html_to_text(page)
    analysis.methods["static_text"] = extract_facts(text)

    if not analysis.fields_available:
        marker = next((m for m in BLOCK_MARKERS if m in lowered), None)
        if marker:
            analysis.blocked = True
            analysis.note = f"bot-protection page ({marker!r})"
        elif len(text) < 1500 or any(m in lowered for m in JS_MARKERS):
            analysis.js_only = True
            analysis.note = "content rendered by JavaScript (little or no static text)"
        else:
            analysis.note = "page readable, but no bonus/fee/earn text matched"
    return analysis


# --------------------------------------------------------------------------
# Page list and fetching
# --------------------------------------------------------------------------


def load_page_config(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def fetch_and_analyze(
    client: httpx.Client, robots: RobotsCache, throttle: HostThrottle, url: str
) -> PageAnalysis:
    allowed, reason = robots.allowed(url)
    if not allowed:
        return PageAnalysis(status_code=None, blocked=True, note=reason)
    throttle.wait(url)
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return PageAnalysis(
            status_code=None, blocked=True, note=f"request failed: {type(exc).__name__}"
        )
    return analyze_page(response.status_code, response.text)


def cross_check(
    card_id: str, analysis: PageAnalysis, api_fee: float | None, api_bonus: float | None
) -> list[dict]:
    """Compare what the issuer page says against the bonuses API."""
    checks = []
    fee = analysis.best("annual_fee")
    if fee is not None and api_fee is not None:
        checks.append(
            {
                "card_id": card_id,
                "field": "annual_fee",
                "api_value": api_fee,
                "page_value": fee["amount"],
                "match": abs(fee["amount"] - api_fee) < 0.5,
                "evidence": fee["evidence"],
            }
        )
    bonus = analysis.best("bonus")
    if bonus is not None and api_bonus is not None:
        checks.append(
            {
                "card_id": card_id,
                "field": "bonus_amount",
                "api_value": api_bonus,
                "page_value": bonus["amount"],
                "match": abs(bonus["amount"] - api_bonus) < 0.5,
                "evidence": bonus["evidence"],
            }
        )
    return checks
