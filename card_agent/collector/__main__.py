"""python -m card_agent.collector run --out DIR [...]"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from card_agent.collector.http import make_client
from card_agent.collector.run import run_collector, summary_markdown, write_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m card_agent.collector")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="fetch sources and write data/ snapshot files")
    run.add_argument("--out", type=Path, required=True, help="directory that holds data/")
    run.add_argument("--rewards-dir", type=Path, help="local clone of fuermosi777/rewards (opt-in)")
    run.add_argument("--source-file", type=Path, help="use a local bonuses API export (offline)")
    run.add_argument("--no-rss", action="store_true", help="skip the Doctor of Credit feed")
    run.add_argument("--no-issuer-pages", action="store_true", help="skip issuer cross-checks")
    run.add_argument("--summary-md", type=Path, help="append a markdown summary here")
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    with make_client() as client:
        snapshot, changes = run_collector(
            args.out,
            client,
            now,
            rewards_dir=args.rewards_dir,
            source_file=args.source_file,
            with_rss=not args.no_rss,
            with_issuer_pages=not args.no_issuer_pages,
        )
    paths = write_outputs(args.out, snapshot, changes)
    summary = summary_markdown(snapshot, changes)
    print(summary)
    for name, path in paths.items():
        print(f"wrote {name}: {path}", file=sys.stderr)
    if args.summary_md:
        with args.summary_md.open("a") as handle:
            handle.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
