"""config/card_details.yaml as a generated artifact: read, write, apply, diff.

The file is written by `dump_details` in a fixed layout (one line per earn
rate or benefit) so a pipeline PR shows exactly what changed. Hand-maintained
keys (define, override, protections, downgrade_to, notes, transferable) are
preserved as they are; the pipeline owns annual_fee, foreign_tx_fee,
point_currency, earn, benefits, evidence and terms_source for cards that have
an issuer page in config/card_sources.yaml.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from card_agent.config import CONFIG_DIR
from card_agent.models import BenefitKind, Cadence, Category
from card_agent.terms.schema import BenefitRow, CardTerms, EarnRow

DETAILS_PATH = CONFIG_DIR / "card_details.yaml"

HEADER = """\
# Card terms the bonuses API doesn't carry: per-category earn rates, annual fees,
# credits/benefits, foreign transaction fees, rewards currency, protections, and
# product-change (downgrade) paths. Also defines a few cards the API lacks.
#
# GENERATED + REVIEWED. For cards with an issuer page in config/card_sources.yaml,
# the card-terms pipeline (card_agent/terms) re-reads the page when it changes,
# extracts the terms with an LLM, checks every value deterministically against a
# verbatim quote from the page, and proposes changes to this file in a PR titled
# "card terms changed: ...". Merge the PR to accept them.
# - Pipeline-owned keys: annual_fee, foreign_tx_fee, point_currency, earn,
#   benefits, evidence, terms_source. Hand edits to these are overwritten by the
#   next accepted extraction; to maintain a card by hand, set its url to null in
#   card_sources.yaml.
# - Hand-owned keys (kept as is): define, override, notes, transferable,
#   protections, downgrade_to.
#
# Conventions
# - Keys are our card ids (issuer slug + name slug, as the collector builds them).
# - multiplier: points/miles per $ for points cards, percent for cash-back cards.
# - Co-brand bonus categories ("6x at Marriott") are left out: they don't map to
#   a general spend category, so the scorer undercounts rather than overcounts.
#   When a category has several rates (portal hotels 10x vs portal flights 5x),
#   the lower one is kept.
# - choice: a menu; the scorer applies the rate to the `choose` categories that
#   are worth the most for your spending (Custom Cash, Customized Cash, Cash+).
# - cap / cap_period: spend cap for that rate; spend above it earns the `other` rate.
# - benefits: amount is USD per year (null when the page states no dollar value).
"""

KEY_ORDER = [
    "define",
    "override",
    "notes",
    "transferable",
    "annual_fee",
    "foreign_tx_fee",
    "point_currency",
    "earn",
    "benefits",
    "protections",
    "downgrade_to",
    "evidence",
    "terms_source",
]
EARN_KEY_ORDER = [
    "category",
    "choice",
    "choose",
    "multiplier",
    "cap",
    "cap_period",
    "notes",
    "evidence",
]
BENEFIT_KEY_ORDER = ["kind", "name", "amount", "cadence", "evidence"]
# Unquoted only if YAML reads it back as the same string: starts with a letter
# (so "2026-10-03" and "123" stay strings) and isn't a YAML keyword.
_PLAIN = re.compile(r"^[a-z][a-z0-9_.+-]*$")


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def load_details(path: Path = DETAILS_PATH) -> dict[str, Any]:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    if _PLAIN.match(text) and text not in ("true", "false", "null", "yes", "no", "on", "off"):
        return text
    return json.dumps(text, ensure_ascii=False)


def _flow(mapping: dict[str, Any], order: list[str]) -> str:
    keys = [k for k in order if k in mapping] + [k for k in mapping if k not in order]
    parts = []
    for key in keys:
        value = mapping[key]
        rendered = (
            "[" + ", ".join(_scalar(v) for v in value) + "]"
            if isinstance(value, list)
            else _scalar(value)
        )
        parts.append(f"{key}: {rendered}")
    return "{" + ", ".join(parts) + "}"


def _emit(key: str, value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        return [f"{pad}{key}:"] + [
            line for sub, subvalue in value.items() for line in _emit(sub, subvalue, indent + 2)
        ]
    if isinstance(value, list):
        if all(not isinstance(item, dict) for item in value):
            return [f"{pad}{key}: [" + ", ".join(_scalar(v) for v in value) + "]"]
        order = EARN_KEY_ORDER if key == "earn" else BENEFIT_KEY_ORDER
        return [f"{pad}{key}:"] + [f"{pad}  - {_flow(item, order)}" for item in value]
    return [f"{pad}{key}: {_scalar(value)}"]


def dump_details(data: dict[str, Any]) -> str:
    lines = [HEADER.rstrip(), "", f"as_of: {_scalar(data.get('as_of'))}", "", "cards:"]
    for card_id, entry in (data.get("cards") or {}).items():
        lines.append(f"  {card_id}:")
        keys = [k for k in KEY_ORDER if k in entry] + [k for k in entry if k not in KEY_ORDER]
        for key in keys:
            lines += _emit(key, entry[key], 4)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_details(data: dict[str, Any], path: Path = DETAILS_PATH) -> None:
    path.write_text(dump_details(data))


# ---------------------------------------------------------------------------
# YAML entry <-> CardTerms
# ---------------------------------------------------------------------------


def terms_from_entry(entry: dict[str, Any]) -> CardTerms:
    """The pipeline-owned fields of one YAML entry."""
    override = entry.get("override") or {}
    define = entry.get("define") or {}
    earn = None
    if "earn" in entry:
        earn = []
        for index, row in enumerate(entry["earn"] or []):
            common = {
                "multiplier": float(row["multiplier"]),
                "cap": row.get("cap"),
                "cap_period": row.get("cap_period"),
                "notes": row.get("notes"),
                "evidence": row.get("evidence"),
            }
            if "choice" in row:
                earn += [
                    EarnRow(
                        category=Category(c),
                        choice_group=f"choice{index}",
                        choose=int(row.get("choose", 1)),
                        **common,
                    )
                    for c in row["choice"]
                ]
            else:
                earn.append(EarnRow(category=Category(row["category"]), **common))
    benefits = None
    if "benefits" in entry:
        benefits = [
            BenefitRow(
                kind=BenefitKind(row["kind"]),
                name=row.get("name", ""),
                amount=row.get("amount"),
                cadence=Cadence(row.get("cadence", "annual")),
                evidence=row.get("evidence"),
            )
            for row in entry["benefits"] or []
        ]
    return CardTerms(
        annual_fee=entry.get("annual_fee", define.get("annual_fee")),
        foreign_tx_fee=entry.get("foreign_tx_fee"),
        point_currency=entry.get(
            "point_currency", override.get("point_currency", define.get("point_currency"))
        ),
        earn=earn,
        benefits=benefits,
        evidence=dict(entry.get("evidence") or {}),
    )


def _row_dict(row: EarnRow) -> dict[str, Any]:
    out: dict[str, Any] = {"category": row.category.value, "multiplier": row.multiplier}
    if row.cap is not None:
        out["cap"] = row.cap
        out["cap_period"] = row.cap_period
    if row.notes:
        out["notes"] = row.notes
    if row.evidence:
        out["evidence"] = row.evidence
    return out


def earn_to_yaml(rows: list[EarnRow]) -> list[dict[str, Any]]:
    """Fixed rows one per line; each choice menu collapsed to one `choice:` line."""
    fixed = [_row_dict(row) for row in rows if not row.choice_group]
    menu_rows = [row for row in rows if row.choice_group]
    if not menu_rows:
        return fixed
    frame = pd.DataFrame(
        {
            "group": [row.choice_group for row in menu_rows],
            "multiplier": [row.multiplier for row in menu_rows],
            "cap": [row.cap for row in menu_rows],
            "cap_period": [row.cap_period for row in menu_rows],
            "row": menu_rows,
        }
    )
    menus = []
    for _key, group in frame.groupby(
        ["group", "multiplier", "cap", "cap_period"], sort=False, dropna=False
    ):
        first = group["row"].iloc[0]
        entry: dict[str, Any] = {
            "choice": [row.category.value for row in group["row"]],
            "choose": first.choose or 1,
            "multiplier": first.multiplier,
        }
        if first.cap is not None:
            entry["cap"] = first.cap
            entry["cap_period"] = first.cap_period
        if first.notes:
            entry["notes"] = first.notes
        if first.evidence:
            entry["evidence"] = first.evidence
        menus.append(entry)
    return fixed + menus


def benefits_to_yaml(rows: list[BenefitRow]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        entry: dict[str, Any] = {
            "kind": row.kind.value,
            "name": row.name,
            "amount": row.amount,
            "cadence": row.cadence.value,
        }
        if row.evidence:
            entry["evidence"] = row.evidence
        out.append(entry)
    return out


def apply_terms(
    entry: dict[str, Any],
    terms: CardTerms,
    source_url: str | None,
    extracted: date,
    model: str | None,
) -> dict[str, Any]:
    """A copy of `entry` with the pipeline-owned fields replaced by `terms`."""
    updated = dict(entry)
    for name in ("annual_fee", "foreign_tx_fee", "point_currency"):
        value = getattr(terms, name)
        if value is not None:
            updated[name] = value
    if terms.point_currency is not None and "override" in updated:
        override = {k: v for k, v in updated["override"].items() if k != "point_currency"}
        if override:
            updated["override"] = override
        else:
            updated.pop("override")
    if terms.earn is not None:
        updated["earn"] = earn_to_yaml(terms.earn)
    if terms.benefits is not None:
        updated["benefits"] = benefits_to_yaml(terms.benefits)
    if terms.evidence:
        updated["evidence"] = dict(sorted(terms.evidence.items()))
    updated["terms_source"] = {
        "url": source_url,
        "extracted": extracted.isoformat(),
        "method": f"llm:{model}" if model else "llm",
    }
    return updated


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass
class DiffRow:
    card_id: str
    field: str
    old: str
    new: str
    change: str  # added | changed | removed
    evidence: str = ""
    url: str = ""

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def _fmt_number(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:g}" if value < 1000 else f"{value:,.0f}"


def _earn_label(rows: list[EarnRow]) -> dict[str, tuple[str, str]]:
    """Canonical key -> (display value, evidence). Choice menus key on their options."""
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not row.choice_group:
            cap = f" up to ${row.cap:,.0f}/{row.cap_period}" if row.cap is not None else ""
            out[f"earn.{row.category.value}"] = (f"{row.multiplier:g}x{cap}", row.evidence or "")
    menu_rows = [row for row in rows if row.choice_group]
    if menu_rows:
        frame = pd.DataFrame({"group": [row.choice_group for row in menu_rows], "row": menu_rows})
        for _group, group in frame.groupby("group", sort=False):
            members = list(group["row"])
            first = members[0]
            options = ", ".join(sorted(row.category.value for row in members))
            cap = f" up to ${first.cap:,.0f}/{first.cap_period}" if first.cap is not None else ""
            shown = f"{first.multiplier:g}x on {first.choose or 1} of the menu{cap}"
            out[f"earn.choice({options})"] = (shown, first.evidence or "")
    return out


def _benefit_label(rows: list[BenefitRow]) -> dict[str, tuple[str, str]]:
    """benefit.<kind> -> (amounts per year, evidence). Keyed by kind, not by the
    model's wording of the name, so rephrasing alone never shows up as a change."""
    if not rows:
        return {}
    frame = pd.DataFrame(
        {
            "kind": [row.kind.value for row in rows],
            "amount": [row.amount or 0.0 for row in rows],
            "evidence": [row.evidence or "" for row in rows],
        }
    ).sort_values(["kind", "amount"])
    out: dict[str, tuple[str, str]] = {}
    for kind, group in frame.groupby("kind", sort=False):
        shown = " + ".join(f"${a:,.0f}/yr" if a else "no $ value" for a in group["amount"])
        out[f"benefit.{kind}"] = (shown, group["evidence"].iloc[0])
    return out


def _fee(value: float) -> str:
    return f"${value:,.0f}"


def _ftf(value: bool) -> str:
    return "charged" if value else "none"


SCALAR_FORMAT = {"annual_fee": _fee, "foreign_tx_fee": _ftf, "point_currency": str}


def diff_terms(card_id: str, old: CardTerms, new: CardTerms, url: str = "") -> list[DiffRow]:
    """Rows where `new` differs from `old`. A scalar `new` lacks isn't compared, nor
    is an earn or benefit list it lacks; within a list, rows can be added,
    changed, or removed."""
    rows: list[DiffRow] = []
    for name, fmt in SCALAR_FORMAT.items():
        new_value, old_value = getattr(new, name), getattr(old, name)
        if new_value is None or new_value == old_value:
            continue
        rows.append(
            DiffRow(
                card_id,
                name,
                "–" if old_value is None else fmt(old_value),
                fmt(new_value),
                "added" if old_value is None else "changed",
                new.evidence.get(name, ""),
                url,
            )
        )
    for new_rows, old_rows, labeler in (
        (new.earn, old.earn, _earn_label),
        (new.benefits, old.benefits, _benefit_label),
    ):
        if new_rows is None:
            continue
        new_map, old_map = labeler(new_rows), labeler(old_rows or [])
        for key in list(new_map) + [k for k in old_map if k not in new_map]:
            if key not in old_map:
                rows.append(
                    DiffRow(card_id, key, "–", new_map[key][0], "added", new_map[key][1], url)
                )
            elif key not in new_map:
                rows.append(DiffRow(card_id, key, old_map[key][0], "–", "removed", "", url))
            elif new_map[key][0] != old_map[key][0]:
                rows.append(
                    DiffRow(
                        card_id,
                        key,
                        old_map[key][0],
                        new_map[key][0],
                        "changed",
                        new_map[key][1],
                        url,
                    )
                )
    return rows


def compare_all_fields(
    card_id: str, hand: CardTerms, extracted: CardTerms, url: str = ""
) -> list[DiffRow]:
    """Every compared field, equal ones included (change="same"): for the bootstrap report."""
    changed = {row.field: row for row in diff_terms(card_id, hand, extracted, url)}
    rows = list(changed.values())
    for name, fmt in SCALAR_FORMAT.items():
        value = getattr(extracted, name)
        if value is not None and name not in changed:
            shown = fmt(value)
            rows.append(
                DiffRow(card_id, name, shown, shown, "same", extracted.evidence.get(name, ""), url)
            )
    for new_rows, labeler in ((extracted.earn, _earn_label), (extracted.benefits, _benefit_label)):
        for key, (shown, evidence) in labeler(new_rows or []).items():
            if key not in changed:
                rows.append(DiffRow(card_id, key, shown, shown, "same", evidence, url))
    return rows
