"""Deterministic validation of an LLM extraction. No LLM here.

A value is kept only if:
- its evidence quote appears in the page text (ignoring whitespace, case, and
  trademark symbols/typographic quotes), and is long enough to mean something;
- the number it claims appears in that quote (3x needs a "3" in the evidence),
  or, for a rate stated once in a heading over a list ("3x points on:" then
  "dining ..."), in a heading quote that precedes the item quote on the page,
  within 1,500 characters, with no other rate stated in between;
- it is within bounds (multiplier 0.5-15, annual fee 0-1000, credits 0-2000/yr);
- its category is one of the existing spend categories.
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
    r"\bno annual fee\b|\$0 annual fee\b|annual fee(?: is|:| of)?\s*(?:none\b|\$0\b)", re.I
)

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


def quote_on_page(quote: str | None, page_key: str) -> bool:
    key = match_key(quote or "")
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
    heading_key, item_key = match_key(heading), match_key(item)
    if not quote_on_page(heading, page_key):
        return "heading not found on page"
    if not quote_on_page(item, page_key):
        return "list item not found on page"
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
            if not 0 <= gap <= MAX_LIST_GAP:
                continue
            between = rates_in(page_key[heading_end:item_start])
            if any(
                abs(value - multiplier) > 1e-6 and (not units or unit in units)
                for value, unit in between
            ):
                nearest = "another rate is stated between the heading and the item"
                continue
            return None
    return nearest or f"heading isn't within {MAX_LIST_GAP:,} characters before the item"


def earn_evidence(row: EarnRateOut) -> str:
    """The quote(s) behind an earn row, as shown in reports."""
    if row.evidence_heading and row.evidence_item:
        return f"{row.evidence_heading} … {row.evidence_item}"
    return row.evidence


def _earn_quote(row: EarnRateOut, page_key: str) -> tuple[str | None, str | None]:
    """(evidence to keep, None) when one quote, or a heading + item pair, backs the
    multiplier; (None, reason) otherwise."""
    single = quote_on_page(row.evidence, page_key)
    if single and number_in(row.multiplier, row.evidence):
        return row.evidence, None
    if row.evidence_heading and row.evidence_item:
        problem = heading_item_problem(
            row.multiplier, row.evidence_heading, row.evidence_item, page_key
        )
        return (None, problem) if problem else (earn_evidence(row), None)
    if not single:
        return None, "evidence not found on page"
    return None, f"evidence doesn't state {row.multiplier:g}"


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
            category=Category(row.category.value),
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
        elif not re.search(r"\bfee\b", fee.evidence, re.I):
            reason = "evidence doesn't mention a fee"
        elif fee.amount == 0 and re.search(r"\bintro|first year", fee.evidence, re.I):
            reason = "an intro/first-year fee is not the ongoing fee"
        elif fee.amount == 0 and not NO_ANNUAL_FEE.search(fee.evidence):
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
        checked, reason = _check_earn(row, page_key)
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
