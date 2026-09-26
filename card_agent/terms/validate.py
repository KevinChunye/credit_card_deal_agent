"""Deterministic validation of an LLM extraction. No LLM here.

A value is kept only if:
- its evidence quote appears in the page text (ignoring whitespace, case, and
  trademark symbols/typographic quotes), and is long enough to mean something;
- the number it claims appears in that quote (3x needs a "3" in the evidence);
- it is within bounds (multiplier 0.5-15, annual fee 0-1000, credits 0-2000/yr);
- its category is one of the existing spend categories.
The whole extraction is rejected if the card named on the page isn't the target
card (multi-card pages such as Amex promos or Bilt's lineup).

Fields that fail keep their previous value and are listed as issues.
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
PERIODS_PER_YEAR = {
    Cadence.monthly: 12,
    Cadence.quarterly: 4,
    Cadence.semiannual: 2,
    Cadence.annual: 1,
    Cadence.one_time: 1,
}
NUMBER_WORDS = {"double": 2, "twice": 2, "triple": 3, "quadruple": 4}

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
    issues: list[Issue] = field(default_factory=list)
    accepted_fields: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # not_listed categories etc. (informational)


def _in_bounds(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _check_earn(row: EarnRateOut, page_key: str) -> tuple[EarnRow | None, str | None]:
    """(row, None) if valid, (None, reason) if not."""
    if not quote_on_page(row.evidence, page_key):
        return None, "evidence not found on page"
    if not _in_bounds(row.multiplier, MULTIPLIER_BOUNDS):
        return None, f"multiplier {row.multiplier:g} outside {MULTIPLIER_BOUNDS}"
    if not number_in(row.multiplier, row.evidence):
        return None, f"evidence doesn't state {row.multiplier:g}"
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
            evidence=row.evidence,
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
        elif fee.amount == 0 and re.search(r"\bintro|first year", fee.evidence, re.I):
            reason = "an intro/first-year fee is not the ongoing fee"
        elif fee.amount == 0 and not re.search(
            r"\bno annual fee|\$0\b|annual fee:?\s*(?:none|\$0)", fee.evidence, re.I
        ):
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
        says_none = re.search(r"\bno\b|\bnone\b|\bwithout\b|\$0\b|\b0%|waived", ftf.evidence, re.I)
        if not quote_on_page(ftf.evidence, page_key):
            reason = "evidence not found on page"
        elif "foreign" not in ftf.evidence.lower():
            reason = "evidence doesn't mention foreign transactions"
        elif ftf.charged == bool(says_none):
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
                    f"earn.{row.category.value}", reason or "", f"{row.multiplier:g}x", row.evidence
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
    result.terms = merge_with_previous(
        terms,
        evidence,
        valid_earn,
        valid_benefits,
        failed_categories,
        failed_kinds,
        result,
        previous,
    )
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


def merge_with_previous(
    terms: CardTerms,
    evidence: dict[str, str],
    valid_earn: list[EarnRow],
    valid_benefits: list[BenefitRow],
    failed_categories: set[Category],
    failed_kinds: set[BenefitKind],
    result: ValidationResult,
    previous: CardTerms,
) -> CardTerms:
    for name in ("annual_fee", "foreign_tx_fee", "point_currency"):
        if name not in result.accepted_fields and getattr(previous, name) is not None:
            setattr(terms, name, getattr(previous, name))
            if name in previous.evidence:
                evidence[name] = previous.evidence[name]
    terms.evidence = evidence

    if valid_earn:
        covered = {row.category for row in valid_earn}
        kept = [
            row
            for row in previous.earn or []
            if row.category in failed_categories and row.category not in covered
        ]
        terms.earn = valid_earn + kept
    else:
        terms.earn = previous.earn

    if "benefits" in result.accepted_fields:
        covered_kinds = {row.kind for row in valid_benefits}
        kept_benefits = [
            row
            for row in previous.benefits or []
            if row.kind in failed_kinds and row.kind not in covered_kinds
        ]
        terms.benefits = valid_benefits + kept_benefits
    else:
        terms.benefits = previous.benefits
    return terms
