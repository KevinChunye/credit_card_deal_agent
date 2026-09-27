"""The trace log: one JSON line per command, next to the state DB.

Each record says what ran (command and safe arguments), how it ended (ok,
error, the `next` instruction), how long it took, and, for `advise`, every
loop step including the Verifier handoffs. `trace` reads it back so you can
ask the agent "how did you decide?" and see the loop.

The log sits next to the state DB and holds nothing the DB doesn't; even so,
the raw JSON passed to `onboard`, `hide` reasons, email content and anything
from the environment are kept out of it. The file keeps the newest
KEEP_LINES records once it passes MAX_BYTES.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

KEEP_LINES = 400
MAX_BYTES = 400_000
# Arguments whose values stay out of the log.
PRIVATE_FLAGS = {"--json", "--from-yaml", "--reason"}


def trace_path(state_dir: Path) -> Path:
    return state_dir / "trace.jsonl"


def safe_argv(argv: list[str]) -> list[str]:
    """argv with private values replaced by "…"."""
    cleaned: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            cleaned.append("…")
            hide_next = False
            continue
        flag, _, value = arg.partition("=")
        if flag in PRIVATE_FLAGS:
            if value:
                cleaned.append(f"{flag}=…")
            else:
                cleaned.append(arg)
                hide_next = True
            continue
        cleaned.append(arg)
    return cleaned


def record(
    state_dir: Path,
    now: datetime,
    argv: list[str],
    payload: dict[str, Any],
    duration_ms: float,
    steps: list[dict] | None = None,
) -> None:
    """Append one record. Never raises: tracing must not break a command."""
    entry = {
        "ts": now.isoformat(),
        "command": payload.get("command"),
        "argv": safe_argv(argv),
        "ok": payload.get("ok"),
        "ms": round(duration_ms),
        "next": payload.get("next"),
        "summary": (payload.get("display_text") or payload.get("error") or "").split("\n")[0][:160],
    }
    if steps:
        entry["steps"] = steps
    path = trace_path(state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        if path.stat().st_size > MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def without_data(step: dict) -> dict:
    return {key: value for key, value in step.items() if key != "data"}


def read(state_dir: Path, last: int = 5, skip_commands: tuple[str, ...] = ("trace",)) -> list[dict]:
    """The newest `last` records, oldest first, leaving out `trace` itself."""
    path = trace_path(state_dir)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line from a crash; skip it
        if entry.get("command") not in skip_commands:
            records.append(entry)
    return records[-last:] if last > 0 else []
