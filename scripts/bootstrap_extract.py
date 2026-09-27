"""Bootstrap: extract every tracked card once and compare with the hand YAML.

    python scripts/bootstrap_extract.py [--out docs/BOOTSTRAP_DIFF.md] [--cards a,b]

Writes a markdown report (card | field | hand value | extracted | evidence) for
spot-checking the hand-compiled config/card_details.yaml. It applies nothing,
commits nothing, and saves no pipeline state. Needs OPENAI_API_KEY; about 52
LLM calls (roughly $0.20 at gpt-6-luna prices).

In GitHub Actions: Actions tab -> Card terms -> Run workflow -> bootstrap: true.
The report is uploaded as the `bootstrap-diff` artifact.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_agent.terms import report as render  # noqa: E402
from card_agent.terms.details import DETAILS_PATH, load_details  # noqa: E402
from card_agent.terms.pipeline import PipelineState, RunOptions  # noqa: E402
from card_agent.terms.runner import card_list, notice, run_pipeline, write_summary  # noqa: E402
from card_agent.terms.sources import SOURCES_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "BOOTSTRAP_DIFF.md")
    parser.add_argument("--cards", help="comma-separated card ids (default: all tracked)")
    parser.add_argument("--summary-md", type=Path, help="append the run summary here")
    parser.add_argument("--details", type=Path, default=DETAILS_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    today = datetime.now(UTC).date()
    options = RunOptions(mode="bootstrap", cards=card_list(args.cards))
    _pipeline, report = run_pipeline(options, PipelineState(), args.details, args.sources, today)
    if report.model is None:
        text = f"# Bootstrap skipped\n\n{report.provider_note}\n"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        write_summary(text, args.summary_md)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render.bootstrap_markdown(report, load_details(args.details)))
    print(f"wrote {args.out}", file=sys.stderr)
    write_summary(
        render.job_summary(report)
        + f"\nFull comparison: `{args.out.name}` (artifact `bootstrap-diff`).\n",
        args.summary_md,
    )
    if report.llm_problem:  # e.g. a rejected key: the report compares nothing
        notice(report.llm_problem, "error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
