"""Deterministic validation of an LLM extraction. No LLM here.

A value is kept only if:
- its evidence quote appears in the page text (ignoring whitespace, case, and
  trademark symbols/typographic quotes), and is long enough to mean something;
- the number it claims appears in that quote (3x needs a "3" in the evidence),
  or, for a rate stated once in a heading over a list ("3x points on:" then
  "dining ..."), in a heading quote that precedes the item quote on the page,
  within 1,500 characters, with no other rate stated in between;
- an earn quote (or its heading or list item) names the rate's category, and a
  rate that only applies to bookings through an issuer's travel portal isn't
  filed as general hotels/flights/travel (what a rate excludes, "travel
  (excluding purchases made through Chase Travel)", doesn't count);
- a capped rate is the base rate only if it covers all purchases ("2% on all
  eligible purchases on up to $50,000 per year"), and a cap its quote mentions
  must be extracted;
- it is within bounds (multiplier 0.5-15, annual fee 0-1000, credits 0-2000/yr);
- its category is one of the existing spend categories.
Benefit amounts are only ever lowered: a coverage limit ("reimbursed up to $800"
if a phone is stolen), a per-use credit ("every time you book") or the cap on a
percentage rebate ("10% back ... up to $250") keeps no dollar value, nor does
a credit unlocked by spending ("$200 Delta Flight Credit after you spend
$10,000") or airline status currency ("$2,500 Medallion Qualification
Dollars"), and a time-limited perk ("when activated by December 31") counts once rather than
every year.
The whole extraction is rejected if the card named on the page isn't the target
card (multi-card pages such as Amex promos or Bilt's lineup).

Fields that fail keep their previous value and are listed as issues. Rows on
file that an extraction doesn't mention are kept too (and listed), since a
removal can't be backed by a quote.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pandas as pd

from card_agent.models import BenefitKind, Cadence, Category
from card_agent.terms.schema import (
    BenefitOut,
    BenefitRow,
    CardTerms,
    EarnRateOut,
    EarnRow,
    TermsExtraction,
)
from card_agent.terms.sources import CardSource, name_matches

MIN_EVIDENCE_CHARS = 8
MULTIPLIER_BOUNDS = (0.5, 15.0)
ANNUAL_FEE_BOUNDS = (0.0, 1000.0)
CREDIT_BOUNDS = (0.0, 2000.0)
CAP_MAX = 1_000_000.0
# Heading + list evidence: the heading must come before the item, at most this
# many characters earlier (whitespace ignored), with no other rate in between.
MAX_LIST_GAP = 1_500
# A one-word list item ("Travel") must sit much closer to its heading.
MIN_ITEM_CHARS = 3
MAX_SHORT_ITEM_GAP = 300
PERIODS_PER_YEAR = {
    Cadence.monthly: 12,
    Cadence.quarterly: 4,
    Cadence.semiannual: 2,
    Cadence.annual: 1,
    Cadence.one_time: 1,
}
NUMBER_WORDS = {"double": 2, "twice": 2, "triple": 3, "quadruple": 4}
# "No annual fee", "$0 annual fee", "Annual fee: $0" -- but not "$0 fraud liability"
# or "report the annual fee as $0".
NO_ANNUAL_FEE = re.compile(
    r"\bno annual (?:credit card |card |membership )?fees?\b"
    r"|\$0 annual fee\b"
    r"|\bannual fee(?: is|:| of)?\s*(?:none\b|\$0\b)"
    r"|\b(?:won't|will not|don't|do not|doesn't|does not|never)(?: have to)? (?:pay|charges?) "
    r"(?:an|any) annual (?:credit card |card |membership )?fees?\b"
    r"|\bno annual,[^.]{0,60}\bfees\b",
    re.I,
)
INTRO_FEE = re.compile(r"\bintro(?:ductory)? (?:annual )?fee|\bfirst year\b", re.I)

# Rewards program -> our currency key. Generic names ("points", "miles",
# "cash back") map to nothing, so they never override a known currency.
CURRENCY_PATTERNS: list[tuple[str, str]] = [
    (r"ultimate rewards", "chase_ur"),
    (r"membership rewards", "amex_mr"),
    (r"thank ?you", "citi_typ"),
    (r"venture miles|capital one miles", "capital_one_miles"),
    (r"bilt (?:points|rewards)", "bilt"),
    (r"wells fargo rewards", "wells_fargo_rewards"),
    (r"hilton honors", "hilton"),
    (r"marriott bonvoy", "marriott"),
    (r"world of hyatt", "hyatt"),
    (r"ihg one rewards|ihg rewards", "ihg"),
    (r"wyndham rewards", "wyndham"),
    (r"choice privileges", "choice"),
    (r"skymiles", "delta"),
    (r"mileageplus", "united"),
    (r"aadvantage", "american"),
    (r"rapid rewards", "southwest"),
    (r"atmos|mileage plan", "alaska"),
    (r"trueblue", "jetblue"),
    (r"avios", "avios"),
    (r"aeroplan", "aeroplan"),
    (r"flying blue", "flying_blue"),
    (r"daily cash", "usd"),
]


# Foreign transaction fee quotes must say where (abroad) and what (a fee/charge).
ABROAD = re.compile(
    r"\bforeign\b|\boutside (?:of )?the (?:united states|u\.? ?s\b\.?)|\binternational\b|\babroad\b",
    re.I,
)
FEE_WORDS = re.compile(r"\bfees?\b|\bcharg(?:e|es|ed|ing)\b|\bpay(?:s|ing)?\b", re.I)
NEGATION = re.compile(
    r"\bno\b|\bnone\b|\bwithout\b|\bwon't\b|\bwill not\b|\bdon't\b|\bdo not\b|"
    r"\bdoesn't\b|\bdoes not\b|\bnever\b|\bzero\b|\$0\b|\b0%|\bwaived\b",
    re.I,
)


# Words that show an earn quote is about the rate's category (match_key form:
# lowercase, no spaces). "other" (the base rate) needs none.
CATEGORY_WORDS: dict[Category, tuple[str, ...]] = {
    Category.dining: ("dining", "dine", "restaurant", "takeout"),
    Category.groceries: ("grocer", "supermarket"),
    Category.online_groceries: ("onlinegrocer", "grocerydelivery", "instacart"),
    Category.gas: ("gas", "fuel"),
    Category.ev_charging: ("electricvehicle", "evcharging", "charging"),
    Category.travel_portal: ("travel", "portal"),
    Category.travel_general: ("travel",),
    Category.flights: ("flight", "airline", "airfare", "airtravel"),
    Category.hotels: ("hotel", "lodging", "resort"),
    Category.transit_rideshare: (
        "transit", "rideshare", "commut", "uber", "lyft", "taxi", "train", "subway", "parking",
        "groundtransport",
    ),
    Category.streaming: ("stream",),
    Category.drugstores: ("drugstore", "pharmac"),
    Category.rent: ("rent", "housing", "mortgage"),
    Category.mobile_wallet: ("wallet", "applepay", "googlepay", "samsungpay"),
    Category.rotating: ("rotating", "quarter", "bonuscategor", "activat"),
}  # fmt: skip
# Rates that apply only to bookings through an issuer's travel portal.
PORTAL = re.compile(
    r"\b(?:chase|capital one|citi|amex|american express|u\.? ?s\.? bank|bank of america|"
    r"wells fargo|bilt|barclays)(?: business)? ?travel\b|\bamextravel|\bcititravel|"
    r"\btravel portal\b|\btravel center\b",
    re.I,
)
DIRECT_BOOKING = re.compile(
    r"\bdirect(?:ly)? (?:from|with|through) (?:the )?(?:airline|hotel)", re.I
)
GENERAL_TRAVEL = {Category.hotels, Category.flights, Category.travel_general}
# What a rate excludes ("travel (excluding purchases made through Chase Travel ...)")
# says nothing about where it applies.
EXCLUSION = re.compile(
    r"\((?:excluding|except|not including|other than)\b[^)]*\)?"
    r"|\b(?:excluding|except|not including|other than)\b[^;.]*",
    re.I,
)
# Nor does the card's own currency ("2 World of Hyatt Bonus Points for each $1").
PROGRAM_POINTS = re.compile(
    r"\b(?:world of hyatt|marriott bonvoy|hilton honors|ihg one rewards|skymiles|mileageplus|"
    r"aadvantage|atmos rewards|rapid rewards|trueblue)(?: bonus)? (?:points?|miles?)\b",
    re.I,
)
# A brand or program in a travel rate makes it a co-brand rate ("at hotels
# participating in Marriott Bonvoy"), which the scorer must not apply to all hotels.
CO_BRAND = re.compile(
    r"\bparticipating\b|\bmarriott\b|\bbonvoy\b|\bhilton\b|\bhyatt\b|\bihg\b|"
    r"\bunited\b(?! states)|\bdelta\b|\bamerican airlines\b|\baadvantage\b|\bjetblue\b|"
    r"\bsouthwest\b|\balaska airlines\b|\batmos\b",
    re.I,
)
# Wording that says a rate is capped; if no cap was extracted, the row is rejected.
CAP_HINT = re.compile(
    r"\bup to the (?:quarterly |annual |monthly )?maximum\b|\bquarterly maximum\b|"
    r"\bon (?:the first|up to) \$[\d,]+",
    re.I,
)
# A capped rate is the base rate only if it covers purchases in general ("2% cash
# back on all eligible purchases on up to $50,000 per calendar year"), not
# purchases somewhere ("... purchases at office supply stores") or in a category.
GENERAL_PURCHASES = re.compile(r"\b(?:all|every|everyday|eligible)(?: \w+){0,2} purchases\b", re.I)
PURCHASE_PLACE = re.compile(r"\bpurchases (?:at|from|with|made)\b", re.I)
# Benefit wording that lowers a stated amount.
COVERAGE = re.compile(
    r"\b(?:protection|insurance|insured|coverage|covered|stolen|damaged|theft|warranty)\b", re.I
)
CREDIT_WORD = re.compile(r"\bcredits?\b", re.I)
# Airline status currency, not money.
STATUS_DOLLARS = re.compile(r"\bqualification dollars?\b|\bMQDs?\b", re.I)
# A credit unlocked by a spending threshold ("after you spend $10,000", "during which
# you spend at least $20,000") costs spend the scorer doesn't model. Small minimum
# purchases ("a stay of $500 or more") are not thresholds.
SPEND_UNLOCK = re.compile(
    r"\bspend(?:s|ing)? (?:at least |over |more than )?\$\d{1,3}(?:,\d{3})+", re.I
)
# "10% back ... up to $250": the amount caps a percentage rebate; it isn't a credit.
REBATE = re.compile(r"\b\d+(?:\.\d+)?% (?:back|cash back|off|discount|savings)\b", re.I)
PER_USE = re.compile(
    r"\b(?:every|each) time\b|\bper (?:booking|stay|reservation|purchase|trip|visit)\b", re.I
)
TIME_LIMITED = re.compile(
    r"\bactivated? by\b|\blimited[- ]time\b|\bfor (?:the first )?\d+ months\b|"
    r"\bfor up to \d+ (?:months|years)\b|\b(?:a|one) year of complimentary\b",
    re.I,
)


def names_category(category: Category, *quotes: str) -> bool:
    """True if one of the quotes names the category (always for the base rate)."""
    words = CATEGORY_WORDS.get(category)
    if not words:
        return True
    keys = [match_key(quote or "") for quote in quotes]
    return any(word in key for word in words for key in keys)


def capped_base(wording: str) -> bool:
    """True if a capped rate filed as the base rate covers all purchases."""
    return (
        GENERAL_PURCHASES.search(wording) is not None
        and PURCHASE_PLACE.search(wording) is None
        and not any(names_category(category, wording) for category in CATEGORY_WORDS)
    )


def plain(text: str) -> str:
    """Text for wording rules: NFKC, straight quotes, single spaces."""
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


def match_key(text: str) -> str:
    """Canonical form for quote matching: no whitespace, marks or case."""
    text = re.sub(r"[®™℠†‡*]", "", text or "")
    text = unicodedata.normalize("NFKC", text)
    for fancy, plain in (
        ("’", "'"),
        ("‘", "'"),
        ("“", '"'),
        ("”", '"'),
        ("–", "-"),
        ("—", "-"),
        ("…", "..."),
    ):
        text = text.replace(fancy, plain)
    return re.sub(r"\s+", "", text).lower()


def quote_key(quote: str | None) -> str:
    """match_key of a quote, without punctuation at either end (a quote that
    stops short of a comma, or adds a full stop, is still verbatim)."""
    return match_key(quote or "").strip(".,;:!?'\"()[]-")


def quote_on_page(quote: str | None, page_key: str) -> bool:
    key = quote_key(quote)
    return len(key) >= MIN_EVIDENCE_CHARS and key in page_key


def numbers_in(text: str) -> set[float]:
    found: set[float] = set()
    for raw, suffix in re.findall(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK]\b)?", text or ""):
        value = float(raw.replace(",", ""))
        found.add(value * 1000 if suffix else value)
    lowered = (text or "").lower()
    found.update(
        value for word, value in NUMBER_WORDS.items() if re.search(rf"\b{word}\b", lowered)
    )
    return found


def number_in(value: float, text: str) -> bool:
    return any(abs(value - found) < 1e-6 for found in numbers_in(text))


def _starts(needle: str, haystack: str) -> list[int]:
    return [match.start() for match in re.finditer(re.escape(needle), haystack)]


def rates_in(key_text: str) -> list[tuple[float, str]]:
    """Rates stated in match_key text: "3x" -> (3.0, "x"), "5%" -> (5.0, "%")."""
    return [
        (float(number), unit)
        for number, unit in re.findall(r"(?<![\d.,$])(\d{1,2}(?:\.\d+)?)(x|%)", key_text)
    ]


def heading_item_problem(multiplier: float, heading: str, item: str, page_key: str) -> str | None:
    """Why a heading + list-item pair doesn't back `multiplier`, or None if it does.

    Both quotes must be on the page, the heading must state the multiplier, the
    item must not state a different one, and some occurrence of the heading must
    end before an occurrence of the item, within MAX_LIST_GAP characters, with no
    different rate (same unit) stated in between (that would be another list)."""
    heading_key, item_key = quote_key(heading), quote_key(item)
    for label, key, shortest in (
        ("heading", heading_key, MIN_EVIDENCE_CHARS),
        ("list item", item_key, MIN_ITEM_CHARS),
    ):
        if len(key) < shortest:
            return f"{label} quote too short"
        if key not in page_key:
            return f"{label} not found on page"
    max_gap = MAX_LIST_GAP if len(item_key) >= MIN_EVIDENCE_CHARS else MAX_SHORT_ITEM_GAP
    if not number_in(multiplier, heading):
        return f"heading doesn't state {multiplier:g}"
    if any(abs(value - multiplier) > 1e-6 for value, _unit in rates_in(item_key)):
        return "list item states a different rate"
    units = {unit for value, unit in rates_in(heading_key) if abs(value - multiplier) < 1e-6}
    nearest = None
    for heading_start in _starts(heading_key, page_key):
        heading_end = heading_start + len(heading_key)
        for item_start in _starts(item_key, page_key):
            gap = item_start - heading_end
            if not 0 <= gap <= max_gap:
                continue
            between = rates_in(page_key[heading_end:item_start])
            if any(
                abs(value - multiplier) > 1e-6 and (not units or unit in units)
                for value, unit in between
            ):
                nearest = "another rate is stated between the heading and the item"
                continue
            return None
    return nearest or f"heading isn't within {max_gap:,} characters before the item"


def earn_evidence(row: EarnRateOut) -> str:
    """The quote(s) behind an earn row, as shown in reports."""
    if row.evidence_heading and row.evidence_item:
        return f"{row.evidence_heading} … {row.evidence_item}"
    return row.evidence


def _earn_quote(row: EarnRateOut, page_key: str) -> tuple[str | None, str | None]:
    """(evidence to keep, None) when one quote, or a heading + item pair, backs the
    multiplier and names the category; (None, reason) otherwise."""
    category = Category(row.category.value)
    single = quote_on_page(row.evidence, page_key)
    single_rate = single and number_in(row.multiplier, row.evidence)
    if single_rate and names_category(category, row.evidence):
        return row.evidence, None
    if row.evidence_heading and row.evidence_item:
        problem = heading_item_problem(
            row.multiplier, row.evidence_heading, row.evidence_item, page_key
        )
        if problem:
            return None, problem
        if not names_category(category, row.evidence_heading, row.evidence_item):
            return None, f"heading and item don't name {category.value}"
        return earn_evidence(row), None
    if not single:
        return None, "evidence not found on page"
    if not single_rate:
        return None, f"evidence doesn't state {row.multiplier:g}"
    return None, f"evidence doesn't name {category.value}"


def map_currency(text: str) -> str | None:
    lowered = (text or "").lower()
    for pattern, key in CURRENCY_PATTERNS:
        if re.search(pattern, lowered):
            return key
    return None


@dataclass
class Issue:
    field: str
    reason: str
    value: str = ""
    evidence: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "reason": self.reason,
            "value": self.value,
            "evidence": self.evidence,
        }


@dataclass
class ValidationResult:
    rejected: bool = False
    reason: str | None = None
    terms: CardTerms | None = None  # merged with previous values; None if rejected
    validated: CardTerms | None = None  # only the values that passed; None if rejected
    issues: list[Issue] = field(default_factory=list)
    accepted_fields: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # not_listed categories etc. (informational)
    # On file but not in this extraction, so kept as they are. A removal can't be
    # backed by a quote, so the pipeline never proposes one; a human decides.
    kept_from_file: list[str] = field(default_factory=list)


def _in_bounds(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _check_earn(row: EarnRateOut, page_key: str) -> tuple[EarnRow | None, str | None]:
    """(row, None) if valid, (None, reason) if not."""
    evidence, problem = _earn_quote(row, page_key)
    if problem:
        return None, problem
    category = Category(row.category.value)
    wording = plain(evidence or "")
    applies = PROGRAM_POINTS.sub(" ", EXCLUSION.sub(" ", wording))  # where the rate applies
    if category in GENERAL_TRAVEL and CO_BRAND.search(applies):
        return None, f"co-brand rate filed as {category.value} (use not_listed)"
    if category in GENERAL_TRAVEL and PORTAL.search(applies) and not DIRECT_BOOKING.search(applies):
        category = Category.travel_portal  # the caller notes the move
    if category == Category.travel_general and not re.search(
        r"\btravel\b", PORTAL.sub(" ", applies), re.I
    ):
        return None, "evidence doesn't name travel in general"
    if category == Category.other and row.cap_usd is not None and not capped_base(wording):
        return None, "a capped base rate must cover all purchases (use a category or not_listed)"
    if row.cap_usd is None and CAP_HINT.search(wording):
        return None, "evidence mentions a spending cap that wasn't extracted"
    if not _in_bounds(row.multiplier, MULTIPLIER_BOUNDS):
        return None, f"multiplier {row.multiplier:g} outside {MULTIPLIER_BOUNDS}"
    cap = cap_period = None
    if row.cap_usd is not None:
        if not (0 < row.cap_usd <= CAP_MAX) or row.cap_period is None:
            return None, f"invalid cap {row.cap_usd} per {row.cap_period}"
        if not (
            quote_on_page(row.cap_evidence, page_key)
            and number_in(row.cap_usd, row.cap_evidence or "")
        ):
            return None, f"cap {row.cap_usd:g} not supported by a quote from the page"
        cap, cap_period = row.cap_usd, row.cap_period
    return (
        EarnRow(
            category=category,
            multiplier=row.multiplier,
            cap=cap,
            cap_period=cap_period,
            choice_group=row.choice_group or None,
            choose=(row.choose or 1) if row.choice_group else None,
            notes=row.description or None,
            evidence=evidence,
        ),
        None,
    )


def _adjust_benefit(row: BenefitOut, result: ValidationResult) -> BenefitOut:
    """Lower what a stated amount is worth when the wording says it isn't a
    recurring credit. Never raises a value. Each change is noted in `skipped`."""
    if row.amount_stated is None or row.kind == BenefitKind.global_entry:
        return row
    wording = plain(row.evidence)
    field = f"benefit.{row.kind.value}"
    if STATUS_DOLLARS.search(wording):
        result.skipped.append(
            f"{field}: ${row.amount_stated:g} is airline status currency, not money; no $ value"
        )
        return row.model_copy(update={"amount_stated": None})
    if SPEND_UNLOCK.search(wording):
        result.skipped.append(
            f"{field}: ${row.amount_stated:g} is unlocked by a spending threshold; no $ value"
        )
        return row.model_copy(update={"amount_stated": None})
    if COVERAGE.search(wording) and not CREDIT_WORD.search(wording):
        result.skipped.append(
            f"{field}: ${row.amount_stated:g} is a coverage limit, not a credit; no $ value"
        )
        return row.model_copy(update={"amount_stated": None})
    if PER_USE.search(wording):
        result.skipped.append(f"{field}: ${row.amount_stated:g} is per use; no yearly $ value")
        return row.model_copy(update={"amount_stated": None})
    if REBATE.search(wording):
        result.skipped.append(
            f"{field}: ${row.amount_stated:g} caps a percentage rebate; no $ value"
        )
        return row.model_copy(update={"amount_stated": None})
    if TIME_LIMITED.search(wording) and row.cadence != Cadence.one_time:
        result.skipped.append(f"{field}: time-limited, so ${row.amount_stated:g} counts once")
        return row.model_copy(update={"cadence": Cadence.one_time})
    return row


def _check_benefit(row: BenefitOut, page_key: str) -> tuple[BenefitRow | None, str | None]:
    if not quote_on_page(row.evidence, page_key):
        return None, "evidence not found on page"
    amount = None
    if row.amount_stated is not None:
        if not number_in(row.amount_stated, row.evidence):
            return None, f"evidence doesn't state ${row.amount_stated:g}"
        if row.kind == BenefitKind.global_entry:
            amount = row.amount_stated / 4  # every ~4 years, annualized like the API data
        else:
            amount = row.amount_stated * PERIODS_PER_YEAR[row.cadence]
        if not _in_bounds(amount, CREDIT_BOUNDS):
            return None, f"${amount:g}/yr outside {CREDIT_BOUNDS}"
    return (
        BenefitRow(
            kind=row.kind,
            name=row.name.strip(),
            amount=round(amount, 2) if amount is not None else None,
            cadence=Cadence.annual if row.kind == BenefitKind.global_entry else row.cadence,
            evidence=row.evidence,
        ),
        None,
    )


def validate_extraction(
    extraction: TermsExtraction,
    page_text: str,
    source: CardSource,
    previous: CardTerms | None,
) -> ValidationResult:
    result = ValidationResult()
    page_key = match_key(page_text)
    previous = previous or CardTerms()

    # 1. Is this even the right card? (multi-card pages)
    name = extraction.card_name_on_page
    if not name_matches(source, name):
        result.rejected = True
        result.reason = f"page card name {name!r} doesn't match {source.name!r} ({', '.join(source.page_names)})"
        return result
    if match_key(name) not in page_key:
        result.rejected = True
        result.reason = f"card name {name!r} not found on the page"
        return result

    terms = CardTerms()
    evidence: dict[str, str] = {}

    # 2. Scalars.
    fee = extraction.annual_fee
    if fee is not None:
        reason = None
        if not quote_on_page(fee.evidence, page_key):
            reason = "evidence not found on page"
        elif not _in_bounds(fee.amount, ANNUAL_FEE_BOUNDS):
            reason = f"${fee.amount:g} outside {ANNUAL_FEE_BOUNDS}"
        elif not re.search(r"\bfees?\b", fee.evidence, re.I):
            reason = "evidence doesn't mention a fee"
        elif fee.amount == 0 and INTRO_FEE.search(plain(fee.evidence)):
            reason = "an intro/first-year fee is not the ongoing fee"
        elif fee.amount == 0 and not NO_ANNUAL_FEE.search(plain(fee.evidence)):
            reason = "evidence doesn't say there is no annual fee"
        elif fee.amount > 0 and not number_in(fee.amount, fee.evidence):
            reason = f"evidence doesn't state ${fee.amount:g}"
        if reason:
            result.issues.append(Issue("annual_fee", reason, f"{fee.amount:g}", fee.evidence))
        else:
            terms.annual_fee = fee.amount
            evidence["annual_fee"] = fee.evidence
            result.accepted_fields.append("annual_fee")

    ftf = extraction.foreign_tx_fee
    if ftf is not None:
        wording = plain(ftf.evidence)
        if not quote_on_page(ftf.evidence, page_key):
            reason = "evidence not found on page"
        elif not ABROAD.search(wording):
            reason = "evidence doesn't mention foreign transactions"
        elif not FEE_WORDS.search(wording):
            reason = "evidence doesn't mention a fee or charge"
        elif ftf.charged == bool(NEGATION.search(wording)):
            reason = "evidence contradicts the value"
        else:
            reason = None
        if reason:
            result.issues.append(Issue("foreign_tx_fee", reason, str(ftf.charged), ftf.evidence))
        else:
            terms.foreign_tx_fee = ftf.charged
            evidence["foreign_tx_fee"] = ftf.evidence
            result.accepted_fields.append("foreign_tx_fee")

    currency = extraction.point_currency
    if currency is not None:
        key = map_currency(currency.name)
        if key is None:
            result.skipped.append(f"point_currency: generic name {currency.name!r} (kept previous)")
        elif not quote_on_page(currency.evidence, page_key):
            result.issues.append(
                Issue(
                    "point_currency", "evidence not found on page", currency.name, currency.evidence
                )
            )
        elif map_currency(currency.evidence) != key:
            result.issues.append(
                Issue(
                    "point_currency",
                    "evidence names a different program",
                    currency.name,
                    currency.evidence,
                )
            )
        else:
            terms.point_currency = key
            evidence["point_currency"] = currency.evidence
            result.accepted_fields.append("point_currency")

    # 3. Earn rates, row by row.
    valid_earn: list[EarnRow] = []
    failed_categories: set[Category] = set()
    for row in extraction.earn_rates:
        if row.category.value == "not_listed":
            result.skipped.append(f"earn: not_listed {row.multiplier:g}x {row.description!r}")
            continue
        if row.choice_group and row.category.value == "other":
            result.skipped.append(
                f"earn: menu option {row.description!r} isn't a spend category; left out"
            )
            continue
        checked, reason = _check_earn(row, page_key)
        if checked is not None and checked.category.value != row.category.value:
            result.skipped.append(
                f"earn.{row.category.value} {row.multiplier:g}x only applies to bookings "
                "through the issuer's travel site; counted as travel_portal"
            )
        if checked is None:
            failed_categories.add(Category(row.category.value))
            result.issues.append(
                Issue(
                    f"earn.{row.category.value}",
                    reason or "",
                    f"{row.multiplier:g}x",
                    earn_evidence(row),
                )
            )
        else:
            valid_earn.append(checked)

    # 4. Benefits, row by row.
    valid_benefits: list[BenefitRow] = []
    failed_kinds: set[BenefitKind] = set()
    for row in extraction.benefits:
        row = _adjust_benefit(row, result)
        checked, reason = _check_benefit(row, page_key)
        if checked is None:
            failed_kinds.add(row.kind)
            amount = (
                f"${row.amount_stated:g} {row.cadence.value}"
                if row.amount_stated is not None
                else ""
            )
            result.issues.append(
                Issue(f"benefit.{row.kind.value}", reason or "", amount, row.evidence)
            )
        else:
            valid_benefits.append(checked)

    valid_earn = resolve_duplicates(valid_earn, result)
    if valid_earn:
        result.accepted_fields.append("earn")
    # An empty benefits list never wipes out known benefits: more likely omitted.
    if valid_benefits:
        result.accepted_fields.append("benefits")

    if not result.accepted_fields:
        result.rejected = True
        result.reason = "no field passed validation"
        return result

    # 5. Merge: failed or missing fields keep their previous value.
    terms.earn = valid_earn or None
    terms.benefits = valid_benefits or None
    terms.evidence = evidence
    result.validated = terms
    result.terms, kept = merge_terms(terms, previous)
    failed = {f"earn.{c.value}" for c in failed_categories}
    failed |= {f"benefit.{k.value}" for k in failed_kinds}
    result.kept_from_file = [key for key in kept if key not in failed]
    return result


def resolve_duplicates(rows: list[EarnRow], result: ValidationResult) -> list[EarnRow]:
    """One rate per category (choice menus excepted), chosen deterministically.

    - An overflow row ("then 1%") beside a capped row for the same category is
      dropped; the cap already sends overflow spend to the base rate.
    - Otherwise the lower multiplier wins (e.g. portal hotels 10x vs portal
      flights 5x -> 5x; "6x on Fri/Sat nights" vs 3x dining -> 3x). Conservative,
      matching the hand-compiled convention.
    """
    fixed = [row for row in rows if not row.choice_group]
    menus = [row for row in rows if row.choice_group]
    if not fixed:
        return _normalize_menus(menus)
    base = min((r.multiplier for r in fixed if r.category == Category.other), default=1.0)
    frame = pd.DataFrame(
        {
            "category": [row.category.value for row in fixed],
            "multiplier": [row.multiplier for row in fixed],
            "capped": [row.cap is not None for row in fixed],
            "row": fixed,
        }
    )
    kept: list[EarnRow] = []
    for category, group in frame.groupby("category", sort=False):
        if len(group) > 1 and group["capped"].any():
            group = group[group["capped"] | (group["multiplier"] > base)]
        choice = group.sort_values("multiplier").iloc[0]
        if len(group) > 1:
            others = ", ".join(f"{m:g}x" for m in group["multiplier"] if m != choice["multiplier"])
            result.skipped.append(
                f"earn.{category}: several rates ({others}); kept the lowest, {choice['multiplier']:g}x"
            )
        kept.append(choice["row"])
    return kept + _normalize_menus(menus)


def _normalize_menus(menus: list[EarnRow]) -> list[EarnRow]:
    """`choose` can't exceed the number of options in its menu."""
    if not menus:
        return []
    sizes = pd.Series([row.choice_group for row in menus]).value_counts().to_dict()
    return [
        row.model_copy(update={"choose": min(row.choose or 1, sizes[row.choice_group])})
        for row in menus
    ]


def uncovered_earn(previous: list[EarnRow], covered: set[Category]) -> list[EarnRow]:
    """Previous rows a new extraction doesn't speak to: fixed rows in categories it
    doesn't cover, and whole choice menus none of whose options it covers."""
    fixed = [row for row in previous if not row.choice_group and row.category not in covered]
    menu_rows = [row for row in previous if row.choice_group]
    if not menu_rows:
        return fixed
    frame = pd.DataFrame(
        {
            "group": [row.choice_group for row in menu_rows],
            "covered": [row.category in covered for row in menu_rows],
        }
    )
    touched = frame.groupby("group")["covered"].any()
    untouched = set(touched[~touched].index)
    # Renamed so a kept menu can't merge with a new menu that has the same label.
    return fixed + [
        row.model_copy(update={"choice_group": f"file:{row.choice_group}"})
        for row in menu_rows
        if row.choice_group in untouched
    ]


def merge_terms(validated: CardTerms, previous: CardTerms) -> tuple[CardTerms, list[str]]:
    """Validated values over previous ones: scalars the extraction lacks, and earn
    categories or benefit kinds it doesn't cover, keep their previous values.
    Returns the merged terms and the earn/benefit keys kept from `previous`.

    The pipeline stores only `validated` and merges it with the current
    card_details.yaml when it diffs, so hand edits to fields an extraction
    didn't validate are never proposed back."""
    merged = CardTerms(evidence=dict(validated.evidence))
    for name in ("annual_fee", "foreign_tx_fee", "point_currency"):
        value = getattr(validated, name)
        if value is None:
            value = getattr(previous, name)
            if value is not None and name in previous.evidence:
                merged.evidence[name] = previous.evidence[name]
        setattr(merged, name, value)

    kept: list[str] = []
    if validated.earn:
        rows = uncovered_earn(previous.earn or [], {row.category for row in validated.earn})
        kept += [f"earn.{row.category.value}" for row in rows]
        merged.earn = validated.earn + rows
    else:
        merged.earn = previous.earn
    if validated.benefits:
        covered = {row.kind for row in validated.benefits}
        others = [row for row in previous.benefits or [] if row.kind not in covered]
        kept += [f"benefit.{row.kind.value}" for row in others]
        merged.benefits = validated.benefits + others
    else:
        merged.benefits = previous.benefits
    return merged, sorted(set(kept))
