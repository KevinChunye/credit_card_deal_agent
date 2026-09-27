"""python -m card_agent.terms <command>

    run    fetch + hash every tracked page; LLM-extract changed/queued ones; save
           state to --data-dir; optionally write card_details.yaml and PR files
    rss    scan the Doctor of Credit feed and queue cards for re-extraction
    smoke  live extraction on a few cards; prints results, saves nothing

Exit status is 0 whenever the run completed, even if some cards failed (their
status says so). A missing OPENAI_API_KEY is not an error: extraction is
skipped with a notice.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from card_agent.terms import report as render
from card_agent.terms import runner
from card_agent.terms.details import DETAILS_PATH, write_details
from card_agent.terms.pipeline import PipelineState, RunOptions
from card_agent.terms.rss_trigger import load_news, scan
from card_agent.terms.runner import (
    card_list,
    log,
    provider_or_note,
    run_pipeline,
    run_url,
    write_summary,
)
from card_agent.terms.sources import SOURCES_PATH, load_sources
from card_agent.terms.state import StateFiles

SMOKE_CARDS = ["chase-sapphire-preferred", "capital-one-venture-x", "amex-gold"]


def cmd_run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    files = StateFiles(args.data_dir)
    state = PipelineState(files.load_hashes(), files.load_terms(), files.load_queue())
    options = RunOptions(
        mode=args.mode,
        cards=card_list(args.cards),
        force=args.force,
        include_pending=args.include_pending,
    )
    pipeline, report = run_pipeline(options, state, args.details, args.sources, now.date())

    state.hashes.updated_at = now
    if any(o.action == "extracted" for o in report.outcomes):
        state.terms.updated_at = now
    files.save(state.hashes, files.hashes_path)
    files.save(state.terms, files.terms_path)
    files.save(state.queue, files.queue_path)
    log(f"saved state to {args.data_dir}")

    names = {card_id: source.name for card_id, source in pipeline.sources.items()}
    pr_note = None
    if report.diffs and args.write_details:
        write_details(pipeline.apply(report), args.details)
        log(f"wrote {args.details} ({len(report.changed_cards)} cards changed)")
        pr_note = "The workflow opens or updates the card-terms PR."
    if report.diffs and args.pr_dir:
        args.pr_dir.mkdir(parents=True, exist_ok=True)
        (args.pr_dir / "title.txt").write_text(render.pr_title(report, names) + "\n")
        (args.pr_dir / "body.md").write_text(render.pr_body(report, names, run_url()))
        (args.pr_dir / "cards.txt").write_text("\n".join(report.changed_cards) + "\n")
    write_summary(render.job_summary(report, names, pr_note), args.summary_md)
    if args.report_json:
        args.report_json.write_text(json.dumps(render.report_json(report), indent=1))
    return 0


def cmd_rss(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    files = StateFiles(args.data_dir)
    queue = files.load_queue()
    sources = load_sources(args.sources)
    with runner.http_client() as client:
        news, origin = load_news(args.data_dir, client, sources, now)
    added = scan(news, sources, queue, now.date())
    files.save(queue, files.queue_path)

    lines = [
        f"## Card terms: RSS trigger, {now:%Y-%m-%d}",
        "",
        f"Checked {len(news)} Doctor of Credit posts (from the {origin}) for a tracked "
        "card named together with a change keyword.",
        "",
    ]
    if added:
        lines += render.table(
            ["card", "post"],
            [
                [
                    render.cell(sources[card_id].name),
                    f"[{render.cell(queue.queued[card_id].reason)}]({queue.queued[card_id].url})",
                ]
                for card_id in added
            ],
        )
        lines += [
            "",
            "These cards are re-extracted by the next step even if their page hash is unchanged.",
        ]
    else:
        lines.append("No new matching posts; nothing queued.")
    if queue.queued:
        lines += ["", f"Queue now: {', '.join(sorted(queue.queued))}"]
    write_summary("\n".join(lines) + "\n", args.summary_md)
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    cards = card_list(args.cards) or SMOKE_CARDS
    provider, note = provider_or_note()
    if provider is None:
        write_summary(
            "## Card-terms smoke test: skipped\n\n"
            f"{note}\n\nPull requests from forks don't receive repository secrets, so "
            "this is expected there.\n",
            args.summary_md,
        )
        return 0
    options = RunOptions(mode="smoke", cards=cards)
    _pipeline, report = run_pipeline(
        options, PipelineState(), args.details, args.sources, datetime.now(UTC).date()
    )
    write_summary(render.smoke_summary(report), args.summary_md)
    if args.report_json:
        args.report_json.write_text(json.dumps(render.report_json(report), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m card_agent.terms")
    parser.add_argument("--details", type=Path, default=DETAILS_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch, hash, extract changed pages, save state")
    run.add_argument("--data-dir", type=Path, required=True, help="the data branch's data/ folder")
    run.add_argument("--mode", choices=["full", "queue"], default="full")
    run.add_argument("--cards", help="comma-separated card ids (default: all tracked)")
    run.add_argument("--force", action="store_true", help="extract even if the page is unchanged")
    run.add_argument(
        "--include-pending",
        action="store_true",
        help="diff every stored extraction (the auto PR is open), not just this run's",
    )
    run.add_argument("--write-details", action="store_true", help="apply changes to the YAML")
    run.add_argument("--pr-dir", type=Path, help="write title.txt/body.md here if anything changed")
    run.add_argument("--summary-md", type=Path, help="append the markdown summary here")
    run.add_argument("--report-json", type=Path, help="write the full run record here")
    run.set_defaults(func=cmd_run)

    rss = sub.add_parser("rss", help="queue cards named in change posts on Doctor of Credit")
    rss.add_argument("--data-dir", type=Path, required=True)
    rss.add_argument("--summary-md", type=Path)
    rss.set_defaults(func=cmd_rss)

    smoke = sub.add_parser("smoke", help="live extraction on a few cards; saves nothing")
    smoke.add_argument(
        "--cards", help=f"comma-separated card ids (default: {','.join(SMOKE_CARDS)})"
    )
    smoke.add_argument("--summary-md", type=Path)
    smoke.add_argument("--report-json", type=Path)
    smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
