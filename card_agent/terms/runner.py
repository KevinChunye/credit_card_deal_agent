"""Shared plumbing for the card-terms commands and scripts/bootstrap_extract.py:
provider selection, one pipeline run with a polite HTTP client, and output."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import httpx

from card_agent.collector.http import make_client
from card_agent.terms.details import load_details
from card_agent.terms.llm import Provider, ProviderUnavailable, get_provider
from card_agent.terms.pipeline import Pipeline, PipelineState, RunOptions, RunReport
from card_agent.terms.sources import load_sources

FETCH_TIMEOUT = 45.0  # some issuer pages take 20-30s from GitHub's runners


def log(message: str) -> None:
    print(f"card-terms: {message}", file=sys.stderr)


def notice(message: str, level: str = "notice") -> None:
    """A log line, and an annotation on the run page when inside GitHub Actions."""
    log(message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title=card terms::{message}")


def provider_or_note() -> tuple[Provider | None, str | None]:
    try:
        return get_provider(), None
    except ProviderUnavailable as exc:
        notice(str(exc))
        return None, str(exc)


def run_url() -> str | None:
    env = os.environ
    if env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    return None


def write_summary(text: str, path: Path | None) -> None:
    print(text)
    if path:
        with path.open("a") as handle:
            handle.write(text + "\n")


def card_list(value: str | None) -> list[str] | None:
    cards = [c.strip() for c in (value or "").split(",") if c.strip()]
    return cards or None


def http_client() -> httpx.Client:
    return make_client(timeout=FETCH_TIMEOUT)


def run_pipeline(
    options: RunOptions,
    state: PipelineState,
    details_path: Path,
    sources_path: Path,
    today: date,
) -> tuple[Pipeline, RunReport]:
    provider, note = provider_or_note()
    with http_client() as client:
        pipeline = Pipeline(
            client=client,
            sources=load_sources(sources_path),
            details=load_details(details_path),
            state=state,
            today=today,
            provider=provider,
            provider_note=note,
        )
        return pipeline, pipeline.run(options)
