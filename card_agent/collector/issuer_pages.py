"""Easy-mode issuer page reading: one plain GET, then look for facts in the
static HTML, JSON-LD, __NEXT_DATA__, or inline state. No JavaScript execution.

Used two ways:
- scripts/probe_issuers.py reports which pages are readable this way.
- The collector fetches only pages marked `cross_check: true` in
  config/card_sources.yaml and cross-checks the bonuses API against them.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

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

# "Strong" matches read like an offer sentence ("Earn 75,000 bonus points after
# you spend $5,000"). "Weak" matches are any amount + unit and pick up noise such
# as cross-promos, referral blurbs, or "minimum transfer is 1,000 points", so
# they are only used when no strong match exists.
_AMOUNT_UNIT = (
    r"(?:(?P<pts>\d{1,3}(?:,\d{3})+)\s+(?:bonus\s+|welcome\s+)?(?:[\w®]+\s+){0,3}?(?P<unit>points|miles)"
    r"|\$(?P<usd>\d{1,3}(?:,\d{3})*)\s+(?:cash\s+|welcome\s+)?(?:rewards?\s+)?"
    r"(?:bonus|cash back|statement credit))"
)
STRONG_BONUS = re.compile(
    r"\b(?:earn|get|receive)\s+(?:an?\s+)?(?:additional\s+)?"
    + _AMOUNT_UNIT
    + r"(?:[^.]{0,120}?\b(?:after|when|once)\s+(?:you\s+)?(?:spend|make|use)"
    + r"|\.\s*\d?\s*just\s+spend)",
    re.I,
)
WEAK_BONUS = re.compile(_AMOUNT_UNIT, re.I)

# Nonzero fees are specific enough to trust. "$0"/"no annual fee" is also used
# in nav menus ("No Annual Fee Cards", "Credit Cards with No Annual Fee") and
# for authorized users ("Additional Cards ... have a $0 annual fee").
STRONG_FEE = (
    re.compile(r"\$([1-9]\d{0,2}(?:,\d{3})?)\s+annual\s+fee", re.I),
    re.compile(
        r"annual\s+(?:membership\s+)?fee(?:\s+(?:is|of))?\s*:?\s*\$([1-9]\d{0,2}(?:,\d{3})?)", re.I
    ),
)
ZERO_FEE = re.compile(
    r"(?<!cards with )(?<!have a )(\bno|\$0)\s+(?:intro(?:ductory)?\s+)?annual\s+fee\b"
    r"(?!\s+(?:credit\s+)?cards?\b|\s*\(|\s+page\b)",
    re.I,
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
    chunks = [chunk for _name, chunk in re.findall(pattern, page, flags=re.S)]
    return [_decode_state(chunk) for chunk in chunks]


def _decode_state(chunk: str) -> str:
    """State blobs are JSON-in-JS, often a JSON string holding escaped JSON."""
    raw = chunk.strip().rstrip(";").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Not plain JSON (e.g. a JS object literal): undo the common escapes.
        return raw.encode("utf-8", "ignore").decode("unicode_escape", "ignore").replace('\\"', '"')
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def flatten_strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for value in obj.values() for s in flatten_strings(value)]
    if isinstance(obj, list):
        return [s for value in obj for s in flatten_strings(value)]
    return []


def _bonus_hit(match: re.Match, text: str, strength: str) -> dict | None:
    if match.group("pts"):
        amount, unit = _to_number(match.group("pts")), match.group("unit").lower()
        if amount < 1000:
            return None
    else:
        amount, unit = _to_number(match.group("usd")), "usd"
        if amount < 50:
            return None
    return {
        "amount": amount,
        "unit": unit,
        "strength": strength,
        "evidence": _evidence(text, match),
    }


def extract_facts(text: str) -> dict[str, list]:
    """Regex the bonus, annual fee and earn rates out of a block of text.

    Each hit carries `strength` ("strong" or "weak") and a short evidence snippet.
    """
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    bonuses = [_bonus_hit(m, text, "strong") for m in STRONG_BONUS.finditer(text)]
    bonuses += [_bonus_hit(m, text, "weak") for m in WEAK_BONUS.finditer(text)]
    fees = [
        {"amount": _to_number(m.group(1)), "strength": "strong", "evidence": _evidence(text, m)}
        for pattern in STRONG_FEE
        for m in pattern.finditer(text)
    ]
    fees += [
        {"amount": 0.0, "strength": "weak", "evidence": _evidence(text, m)}
        for m in ZERO_FEE.finditer(text)
    ]
    earn = [
        {
            "rate": float(m.group(1)),
            "on": m.group(2).strip(" ,&-").lower(),
            "evidence": _evidence(text, m),
        }
        for pattern in EARN_PATTERNS
        for m in pattern.finditer(text)
    ]
    return {
        "bonus": [hit for hit in bonuses if hit][:6],
        "annual_fee": fees[:6],
        "earn_rates": earn[:10],
    }


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
        """The most trustworthy hit for a field: strong beats weak, then visible
        page text beats structured blobs (which often hold other cards' promos)."""
        order = ("static_text", "json_ld", "next_data", "inline_state")
        hits = [
            hit for method in order for hit in self.methods.get(method, {}).get(field_name) or []
        ]
        strong = [hit for hit in hits if hit.get("strength", "strong") == "strong"]
        return (strong or hits or [None])[0]

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
    if status_code is not None and status_code >= 400:
        analysis.note = f"HTTP {status_code}: page not found; the URL may have moved"
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


def fetch_html(
    client: httpx.Client, robots: RobotsCache, throttle: HostThrottle, url: str
) -> tuple[int | None, str, str | None]:
    """One polite GET: (status code, body, error). Robots.txt first, then throttle."""
    allowed, reason = robots.allowed(url)
    if not allowed:
        return None, "", reason
    throttle.wait(url)
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return None, "", f"request failed: {type(exc).__name__}"
    return response.status_code, response.text, None


def fetch_and_analyze(
    client: httpx.Client, robots: RobotsCache, throttle: HostThrottle, url: str
) -> PageAnalysis:
    status, body, error = fetch_html(client, robots, throttle, url)
    if error:
        return PageAnalysis(status_code=None, blocked=True, note=error)
    return analyze_page(status, body)


def cross_check(
    card_id: str,
    analysis: PageAnalysis,
    api_fee: float | None,
    api_bonus: float | None,
    api_unit: str | None = None,
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
    if bonus is not None and bonus["strength"] != "strong":
        bonus = None  # weak bonus hits are too noisy to compare
    if bonus is not None and api_unit and (bonus["unit"] == "usd") != (api_unit == "usd"):
        bonus = None  # "$200 bonus" vs 20,000 points: same offer, different units
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
