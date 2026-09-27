"""Markdown for the job summary, the auto PR, the smoke test, and the bootstrap
report, plus a JSON record of the run. Everything shown comes from the run."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from card_agent.terms.details import compare_all_fields, terms_from_entry
from card_agent.terms.pipeline import ACTIONS, CardOutcome, RunReport
from card_agent.terms.schema import TermsExtraction
from card_agent.terms.validate import ValidationResult, earn_evidence, map_currency

MAX_PR_BODY = 60_000  # GitHub rejects bodies over 65,536 characters
EVIDENCE_CHARS = 220
PR_BRANCH = "card-terms/auto"


# Page text shown in the PR must stay inert: no @mentions (they notify people),
# no #123 issue links, no markdown links or HTML.
INERT = [
    ("|", "\\|"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ("[", "\\["),
    ("]", "\\]"),
    ("@", "@\u200b"),
    ("#", "#\u200b"),
]


def cell(value: Any, limit: int | None = None) -> str:
    """One markdown table cell: single line, inert, optionally truncated."""
    text = " ".join(str("" if value is None else value).split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    for char, safe in INERT:
        text = text.replace(char, safe)
    return text


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def quote(text: str | None) -> str:
    return f"“{cell(text, EVIDENCE_CHARS)}”" if text else ""


def link(url: str | None) -> str:
    return f"[{urlsplit(url).netloc}]({url})" if url else ""


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def usage_line(report: RunReport) -> str:
    if report.model is None:
        return f"**LLM:** not used. {report.provider_note or ''}".strip()
    if report.llm_problem:
        return f"**LLM problem:** {report.llm_problem} Extraction stopped for this run."
    usage, cost = report.usage, report.cost
    cost_text = (
        f"${cost:.4f}"
        if cost is not None
        else "unknown (model not in the price table; set LLM_PRICE_INPUT_PER_MTOK and "
        "LLM_PRICE_OUTPUT_PER_MTOK)"
    )
    return (
        f"**LLM:** `{report.model}` ({report.provider}) · calls: {usage.calls} · "
        f"input tokens: {usage.input_tokens:,} ({usage.cached_input_tokens:,} cached) · "
        f"output tokens: {usage.output_tokens:,} ({usage.reasoning_tokens:,} reasoning) · "
        f"estimated cost: {cost_text}"
    )


def validation_rows(outcomes: list[CardOutcome]) -> list[list[str]]:
    """The validation report: rejected extractions and fields that failed checks."""
    rows: list[list[str]] = []
    for outcome in outcomes:
        validation = outcome.validation
        if validation is None:
            continue
        name = cell(outcome.source.name)
        if validation.rejected:
            shown = outcome.extraction.card_name_on_page if outcome.extraction else ""
            rows.append([name, "whole extraction", cell(validation.reason), cell(shown), ""])
        rows += [
            [name, cell(issue.field), cell(issue.reason), cell(issue.value), quote(issue.evidence)]
            for issue in validation.issues
        ]
    return rows


VALIDATION_HEADERS = ["card", "field", "problem", "value", "evidence"]


def kept_rows(outcomes: list[CardOutcome], card_ids: set[str] | None = None) -> list[str]:
    lines = []
    for outcome in outcomes:
        validation = outcome.validation
        if validation is None or not validation.kept_from_file:
            continue
        if card_ids is not None and outcome.card_id not in card_ids:
            continue
        lines.append(f"- {outcome.source.name}: {', '.join(validation.kept_from_file)}")
    return lines


# ---------------------------------------------------------------------------
# Job summary
# ---------------------------------------------------------------------------


def job_summary(
    report: RunReport, names: dict[str, str] | None = None, pr_note: str | None = None
) -> str:
    names = names or {o.card_id: o.source.name for o in report.outcomes}
    lines = [
        f"## Card terms: {report.mode} run, {report.today:%Y-%m-%d}",
        "",
        usage_line(report),
        "",
    ]
    if not report.outcomes:
        lines.append(
            "No cards to process."
            if report.mode != "queue"
            else "No cards queued by the RSS trigger; nothing fetched, no LLM calls."
        )
        return "\n".join(lines) + "\n"

    counts = pd.Series([o.action for o in report.outcomes]).value_counts()
    lines += table(
        ["outcome", "cards"],
        [[ACTIONS[action], str(counts[action])] for action in ACTIONS if action in counts],
    )
    lines.append("")
    if report.diffs:
        changed = ", ".join(names.get(c, c) for c in report.changed_cards)
        lines.append(
            f"**Proposed changes:** {plural(len(report.diffs), 'field')} on "
            f"{plural(len(report.changed_cards), 'card')}: {changed}. {pr_note or ''}".rstrip()
        )
    else:
        lines.append(
            "**No changes** to config/card_details.yaml; only page hashes and "
            "last_verified dates were updated."
        )
    flagged = validation_rows(report.outcomes)
    if flagged:
        lines += ["", "### Validation report", ""]
        lines += table(VALIDATION_HEADERS, flagged)
    kept = kept_rows(report.outcomes)
    if kept:
        lines += [
            "",
            "<details><summary>On file but not in this run's extractions (kept)</summary>",
            "",
        ]
        lines += kept + ["", "</details>"]
    if report.queued:
        lines += ["", "Still queued by the RSS trigger: " + ", ".join(sorted(report.queued))]

    per_card = [
        [
            cell(o.source.name),
            ACTIONS.get(o.action, o.action),
            o.status,
            cell(o.why, 80),
            f"{o.text_chars:,}",
            f"{o.usage.input_tokens:,} / {o.usage.output_tokens:,}" if o.usage.calls else "",
            cell(o.note, 160),
        ]
        for o in report.outcomes
    ]
    lines += ["", f"<details><summary>Per card ({len(per_card)})</summary>", ""]
    lines += table(
        ["card", "outcome", "status", "why extracted", "page chars", "tokens in / out", "note"],
        per_card,
    )
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Auto PR
# ---------------------------------------------------------------------------


def pr_title(report: RunReport, names: dict[str, str]) -> str:
    changed = [names.get(card_id, card_id) for card_id in report.changed_cards]
    shown = ", ".join(changed[:3])
    if len(changed) > 3:
        shown += f" and {len(changed) - 3} more"
    return f"card terms changed: {shown}"[:250]


def pr_body(report: RunReport, names: dict[str, str], run_url: str | None = None) -> str:
    run = f" ([workflow run]({run_url}))" if run_url else ""
    head = [
        f"Proposed by the card-terms pipeline{run} on {report.today:%Y-%m-%d}.",
        "",
        f"Each new value was read off the issuer's product page by `{report.model or 'the LLM'}` "
        "and then passed deterministic checks (no LLM): the evidence quote appears on the "
        "page (whitespace-insensitive), the number is in the quote, the value is within "
        "bounds, the category is a known one, and the page names this card. Please check "
        "the quotes against the source pages before merging.",
        "",
    ]
    rows = [
        [
            cell(names.get(row.card_id, row.card_id)),
            cell(row.field),
            cell(row.old),
            cell(row.new),
            quote(row.evidence),
            link(row.url),
        ]
        for row in report.diffs
    ]
    tail = []
    flagged = validation_rows(report.outcomes)
    if flagged:
        tail += ["", "### Validation report", ""]
        tail += ["Fields below failed validation and keep their current values.", ""]
        tail += table(VALIDATION_HEADERS, flagged)
    kept = kept_rows(report.outcomes, set(report.changed_cards))
    if kept:
        tail += [
            "",
            "### On file but not found in the extraction (kept)",
            "",
            "The pipeline never removes a rate or credit on its own. If the issuer "
            "dropped one of these, remove it by hand.",
            "",
        ] + kept
    tail += [
        "",
        "### How this PR works",
        "",
        f"- Branch `{PR_BRANCH}` is regenerated on every pipeline run while this PR is "
        "open; commits pushed to it are overwritten. To adjust a value, merge and then "
        "edit `config/card_details.yaml` on main.",
        "- Closing this PR declines it: a card is proposed again only after its page changes.",
        "- Hashes, last_verified dates, and the full extraction with evidence are on the "
        "`data` branch (`data/page_hashes.json`, `data/card_terms.json`).",
        "",
        "_Opened by `.github/workflows/card_terms.yml`._",
    ]
    budget = MAX_PR_BODY - len("\n".join(head + tail)) - 400
    body_table = table(["card", "field", "old", "new", "evidence quote", "source URL"], [])
    shown = 0
    for row in rows:
        line = "| " + " | ".join(row) + " |"
        if budget - len(line) - 1 < 0:
            break
        body_table.append(line)
        budget -= len(line) + 1
        shown += 1
    if shown < len(rows):
        body_table += [
            "",
            f"_…and {len(rows) - shown} more rows: see the job summary or the "
            "`card-terms-report` artifact of the workflow run._",
        ]
    return "\n".join(head + body_table + tail) + "\n"


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def _earn_value(row) -> str:
    cap = f" up to ${row.cap_usd:,.0f}/{row.cap_period}" if row.cap_usd is not None else ""
    menu = f" (menu {row.choice_group!r}, choose {row.choose or 1})" if row.choice_group else ""
    return f"{row.multiplier:g}x{cap}{menu}: {row.description}"


def extraction_rows(
    extraction: TermsExtraction, validation: ValidationResult | None
) -> list[list[str]]:
    """What the model returned, field by field, with what validation made of it."""
    issues = validation.issues if validation else []
    failures = {(issue.field, issue.evidence): issue.reason for issue in issues}
    rejected = validation is not None and validation.rejected

    def verdict(name: str, evidence: str, skipped: str | None = None) -> str:
        if rejected:
            return "not used (extraction rejected)"
        if skipped:
            return f"skipped ({skipped})"
        if (name, evidence) in failures:
            return f"failed: {failures[(name, evidence)]}"
        return "accepted"

    rows: list[list[str]] = []
    fee, ftf, currency = extraction.annual_fee, extraction.foreign_tx_fee, extraction.point_currency
    if fee is not None:
        rows.append(
            [
                "annual_fee",
                f"${fee.amount:g}",
                quote(fee.evidence),
                verdict("annual_fee", fee.evidence),
            ]
        )
    if ftf is not None:
        shown = "charged" if ftf.charged else "none"
        rows.append(
            ["foreign_tx_fee", shown, quote(ftf.evidence), verdict("foreign_tx_fee", ftf.evidence)]
        )
    if currency is not None:
        generic = None if map_currency(currency.name) else "generic name; kept the file's"
        rows.append(
            [
                "point_currency",
                cell(currency.name),
                quote(currency.evidence),
                verdict("point_currency", currency.evidence, generic),
            ]
        )
    for row in extraction.earn_rates:
        name = f"earn.{row.category.value}"
        skipped = "not a tracked category" if row.category.value == "not_listed" else None
        rows.append(
            [
                name,
                cell(_earn_value(row), 120),
                quote(earn_evidence(row)),
                verdict(name, earn_evidence(row), skipped),
            ]
        )
    for row in extraction.benefits:
        name = f"benefit.{row.kind.value}"
        amount = (
            f"${row.amount_stated:g} {row.cadence.value}"
            if row.amount_stated is not None
            else "no $ value"
        )
        rows.append(
            [
                name,
                cell(f"{row.name}: {amount}", 120),
                quote(row.evidence),
                verdict(name, row.evidence),
            ]
        )
    return rows


def smoke_summary(report: RunReport) -> str:
    lines = [
        f"## Card-terms smoke test: live extraction on {plural(len(report.outcomes), 'card')}",
        "",
        "Nothing is committed and no PR is opened; this shows what the pipeline would "
        "extract and how validation judges it.",
        "",
        usage_line(report),
        "",
    ]
    diffs = pd.DataFrame([row.as_dict() for row in report.diffs])
    for outcome in report.outcomes:
        lines += [f"### {outcome.source.name}: {ACTIONS.get(outcome.action, outcome.action)}", ""]
        facts = [link(outcome.source.url), f"{outcome.text_chars:,} chars of page text"]
        if outcome.sha256:
            facts.append(f"sha256 `{outcome.sha256[:12]}`")
        if outcome.usage.calls:
            facts.append(
                f"{outcome.usage.input_tokens:,} input / {outcome.usage.output_tokens:,} output tokens"
            )
        lines += [" · ".join(facts), ""]
        if outcome.note:
            lines += [f"Note: {outcome.note}", ""]
        extraction, validation = outcome.extraction, outcome.validation
        if extraction is None:
            continue
        verdict = (
            f"rejected: {validation.reason}"
            if validation and validation.rejected
            else "matches this card"
        )
        lines += [f"Card named on the page: “{cell(extraction.card_name_on_page)}” ({verdict})", ""]
        lines += table(
            ["field", "extracted", "evidence", "validation"],
            extraction_rows(extraction, validation),
        )
        if validation and validation.skipped:
            lines += ["", "Skipped: " + "; ".join(cell(s) for s in validation.skipped)]
        if validation and validation.kept_from_file:
            lines += [
                "",
                "On file but not in this extraction (kept): "
                + ", ".join(validation.kept_from_file),
            ]
        card_diffs = diffs[diffs["card_id"] == outcome.card_id] if not diffs.empty else diffs
        lines += ["", "**Compared with config/card_details.yaml:**", ""]
        if validation is None or validation.rejected:
            lines.append("not compared (extraction rejected)")
        elif card_diffs.empty:
            lines.append("no differences")
        else:
            lines += table(
                ["field", "file", "extracted", "change"],
                [
                    [cell(r.field), cell(r.old), cell(r.new), r.change]
                    for r in card_diffs.itertuples()
                ],
            )
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def bootstrap_markdown(report: RunReport, details: dict[str, Any]) -> str:
    """Extraction vs the hand-compiled YAML, field by field, for spot-checking."""
    compared = []
    for outcome in report.outcomes:
        if outcome.action != "extracted" or outcome.validation is None:
            continue
        entry = (details.get("cards") or {}).get(outcome.card_id)
        if entry is None or outcome.validation.terms is None:
            continue
        compared += compare_all_fields(
            outcome.card_id,
            terms_from_entry(entry),
            outcome.validation.terms,
            outcome.source.url or "",
        )
    names = {o.card_id: o.source.name for o in report.outcomes}
    frame = pd.DataFrame(
        [row.as_dict() for row in compared],
        columns=["card_id", "field", "old", "new", "change", "evidence", "url"],
    )
    frame["card"] = frame["card_id"].map(names)
    order = {o.card_id: i for i, o in enumerate(report.outcomes)}
    frame["order"] = frame["card_id"].map(order)
    frame = frame.sort_values(["order", "field"], kind="stable")

    extracted = sum(o.action == "extracted" for o in report.outcomes)
    lines = [
        "# Bootstrap: LLM extraction vs the hand-compiled card_details.yaml",
        "",
        f"Generated {report.today:%Y-%m-%d} by `scripts/bootstrap_extract.py` over "
        f"{len(report.outcomes)} tracked cards ({extracted} extracted and validated). "
        "Nothing was applied: this is for spot-checking the hand-compiled data.",
        "",
        usage_line(report),
        "",
        "How to read it: **hand value** is `config/card_details.yaml`; **extracted** is what "
        "the model read off the issuer page *and* what passed validation (quote found on the "
        "page, number in the quote, bounds, right card). Fields that failed validation are "
        "listed separately and are not compared. `–` means absent.",
        "",
        "## Summary per card",
        "",
    ]
    counts = (
        frame.groupby(["card_id", "change"]).size().unstack(fill_value=0)
        if not frame.empty
        else pd.DataFrame()
    )

    def count(card_id: str, change: str) -> str:
        if card_id not in counts.index or change not in counts.columns:
            return "0"
        return str(int(counts.loc[card_id, change]))

    summary_rows = []
    for outcome in report.outcomes:
        failed = str(len(outcome.validation.issues)) if outcome.validation else ""
        if outcome.action == "extracted":
            tallies = [count(outcome.card_id, c) for c in ("same", "changed", "added", "removed")]
        else:
            tallies = ["", "", "", ""]
        summary_rows.append(
            [cell(outcome.source.name), ACTIONS.get(outcome.action, outcome.action)]
            + tallies
            + [failed]
        )
    lines += table(
        ["card", "outcome", "same", "differ", "extracted only", "hand only", "failed validation"],
        summary_rows,
    )

    differences = frame[frame["change"] != "same"]
    lines += ["", f"## Differences ({len(differences)})", ""]
    lines += table(
        ["card", "field", "hand value", "extracted", "evidence"],
        [
            [cell(r.card), cell(r.field), cell(r.old), cell(r.new), quote(r.evidence)]
            for r in differences.itertuples()
        ],
    )
    flagged = validation_rows(report.outcomes)
    lines += ["", f"## Failed validation ({len(flagged)})", ""]
    lines += table(VALIDATION_HEADERS, flagged) if flagged else ["None."]
    missing = [
        [cell(o.source.name), ACTIONS.get(o.action, o.action), cell(o.note, 200)]
        for o in report.outcomes
        if o.action != "extracted"
    ]
    lines += ["", f"## Not compared ({len(missing)})", ""]
    lines += table(["card", "outcome", "reason"], missing) if missing else ["None."]
    same = frame[frame["change"] == "same"]
    lines += ["", f"<details><summary>Matching fields ({len(same)})</summary>", ""]
    lines += table(
        ["card", "field", "value", "evidence"],
        [[cell(r.card), cell(r.field), cell(r.new), quote(r.evidence)] for r in same.itertuples()],
    )
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# JSON record (artifact)
# ---------------------------------------------------------------------------


def report_json(report: RunReport) -> dict[str, Any]:
    return {
        "mode": report.mode,
        "date": report.today.isoformat(),
        "provider": report.provider,
        "model": report.model,
        "provider_note": report.provider_note,
        "usage": report.usage.__dict__,
        "estimated_cost_usd": report.cost,
        "outcomes": [
            {
                "card_id": o.card_id,
                "url": o.source.url,
                "action": o.action,
                "source_status": o.status,
                "why": o.why,
                "note": o.note,
                "text_chars": o.text_chars,
                "sha256": o.sha256,
                "usage": o.usage.__dict__,
                "extraction": o.extraction.model_dump(mode="json") if o.extraction else None,
                "rejected": o.validation.rejected if o.validation else None,
                "issues": [i.as_dict() for i in o.validation.issues] if o.validation else [],
                "kept_from_file": o.validation.kept_from_file if o.validation else [],
                "skipped": o.validation.skipped if o.validation else [],
            }
            for o in report.outcomes
        ],
        "diffs": [row.as_dict() for row in report.diffs],
        "still_queued": report.queued,
    }
