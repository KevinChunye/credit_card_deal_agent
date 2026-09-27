"""`python -m card_agent.terms` and scripts/bootstrap_extract.py, offline, plus the
markdown they produce (job summary, PR, smoke test, bootstrap report)."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from card_agent.models import NewsItem, Snapshot
from card_agent.terms import report as render
from card_agent.terms import runner
from card_agent.terms.__main__ import main
from card_agent.terms.details import DiffRow, dump_details, load_details
from card_agent.terms.llm import LLMError, Usage
from card_agent.terms.pipeline import RunReport
from card_agent.terms.state import StateFiles
from tests.conftest import routed_client
from tests.terms_fakes import (
    PAGES,
    TODAY,
    FakeProvider,
    csp_extraction,
    gold_extraction,
    hand_details,
    sources,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path):
    """card_details.yaml and card_sources.yaml stand-ins, plus a data dir."""
    details = tmp_path / "card_details.yaml"
    details.write_text(dump_details(hand_details()))
    source_file = tmp_path / "card_sources.yaml"
    source_file.write_text(
        yaml.safe_dump(
            {
                "cards": {
                    card_id: source.model_dump(exclude={"card_id"})
                    for card_id, source in sources().items()
                }
            }
        )
    )
    data = tmp_path / "data"
    data.mkdir()
    return {"details": details, "sources": source_file, "data": data, "tmp": tmp_path}


@pytest.fixture
def offline(monkeypatch):
    """Serve the fixture pages instead of the web, with no API key in the env."""
    monkeypatch.setattr(runner, "http_client", lambda: routed_client(dict(PAGES))[0])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(runner, "get_provider", lambda: provider)


def cli(config, *args):
    return main(["--details", str(config["details"]), "--sources", str(config["sources"]), *args])


def test_run_without_api_key_skips_extraction_and_exits_zero(config, offline, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    summary = config["tmp"] / "summary.md"
    pr_dir = config["tmp"] / "pr"
    before = config["details"].read_text()

    code = cli(
        config,
        "run",
        "--data-dir",
        str(config["data"]),
        "--write-details",
        "--pr-dir",
        str(pr_dir),
        "--summary-md",
        str(summary),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "::notice title=card terms::OPENAI_API_KEY is not set" in out
    assert "OPENAI_API_KEY is not set" in summary.read_text()
    assert "needs extraction, no LLM configured" in summary.read_text()

    hashes = StateFiles(config["data"]).load_hashes()
    assert hashes.cards["amex-gold"].last_fetched is not None
    assert hashes.cards["amex-gold"].sha256 is None  # extracted once a key is set
    assert hashes.cards["citi-custom-cash"].source_status == "manual"
    assert not pr_dir.exists()
    assert config["details"].read_text() == before


def test_run_writes_yaml_pr_files_and_state(config, offline, monkeypatch):
    palladium = csp_extraction(card_name_on_page="Bilt Palladium Card")
    use_provider(
        monkeypatch,
        FakeProvider(
            {
                "chase-sapphire-preferred": csp_extraction(),
                "amex-gold": gold_extraction(),
                "wells-fargo-bilt": palladium,
            }
        ),
    )
    summary = config["tmp"] / "summary.md"
    pr_dir = config["tmp"] / "pr"
    record = config["tmp"] / "report.json"
    code = cli(
        config,
        "run",
        "--data-dir",
        str(config["data"]),
        "--write-details",
        "--pr-dir",
        str(pr_dir),
        "--summary-md",
        str(summary),
        "--report-json",
        str(record),
    )
    assert code == 0

    assert (pr_dir / "title.txt").read_text() == "card terms changed: American Express Gold Card\n"
    body = (pr_dir / "body.md").read_text()
    assert "| card | field | old | new | evidence quote | source URL |" in body
    assert (
        "| American Express Gold Card | annual_fee | $250 | $325 | “Annual Fee: $325.” | "
        "[www.americanexpress.com](https://www.americanexpress.com/us/credit-cards/card/gold-card/) |"
    ) in body
    assert "### Validation report" in body and "Bilt Palladium Card" in body
    assert (pr_dir / "cards.txt").read_text() == "amex-gold\n"

    details = load_details(config["details"])
    assert details["cards"]["amex-gold"]["annual_fee"] == 325
    assert details["cards"]["amex-gold"]["terms_source"]["method"] == "llm:gpt-6-luna"
    assert (
        details["cards"]["chase-sapphire-preferred"]
        == hand_details()["cards"]["chase-sapphire-preferred"]
    )

    files = StateFiles(config["data"])
    assert set(files.load_terms().cards) == {"chase-sapphire-preferred", "amex-gold"}
    assert files.load_hashes().cards["wells-fargo-bilt"].source_status == "validation_failed"

    text = summary.read_text()
    assert "estimated cost: $" in text and "calls: 3" in text
    assert "**Proposed changes:** 2 fields on 1 card: American Express Gold Card." in text
    assert json.loads(record.read_text())["usage"]["calls"] == 3


def test_second_run_on_unchanged_pages_changes_nothing(config, offline, monkeypatch):
    use_provider(
        monkeypatch,
        FakeProvider(
            {
                "chase-sapphire-preferred": csp_extraction(),
                "amex-gold": gold_extraction(),
                "wells-fargo-bilt": csp_extraction(card_name_on_page="Bilt Blue Card"),
            }
        ),
    )
    assert cli(config, "run", "--data-dir", str(config["data"])) == 0

    idle = FakeProvider({})
    use_provider(monkeypatch, idle)
    pr_dir = config["tmp"] / "pr"
    assert cli(config, "run", "--data-dir", str(config["data"]), "--pr-dir", str(pr_dir)) == 0
    assert idle.calls == []
    assert not pr_dir.exists()  # nothing new, and no open PR to refresh


def test_rss_command_queues_cards_from_the_collector_snapshot(config, offline):
    now = datetime.now(UTC)
    snapshot = Snapshot(
        generated_at=now,
        news=[
            NewsItem(
                title="Amex Gold Card Annual Fee Increasing To $350, New Benefits",
                url="https://www.doctorofcredit.com/amex-gold-changes/",
                published_at=now,
            ),
            NewsItem(
                title="Chase Sapphire Preferred 75,000 Points Signup Bonus",
                url="https://www.doctorofcredit.com/csp-75k/",
                published_at=now,
            ),
        ],
    )
    (config["data"] / "latest.json").write_text(snapshot.model_dump_json())
    summary = config["tmp"] / "summary.md"

    assert cli(config, "rss", "--data-dir", str(config["data"]), "--summary-md", str(summary)) == 0
    queue = StateFiles(config["data"]).load_queue()
    assert list(queue.queued) == ["amex-gold"]
    text = summary.read_text()
    assert "Checked 2 Doctor of Credit posts (from the collector snapshot" in text
    assert (
        "[Amex Gold Card Annual Fee Increasing To $350, New Benefits](https://www.doctorofcredit.com/amex-gold-changes/)"
        in text
    )


def test_smoke_skips_cleanly_without_a_key(config, offline):
    summary = config["tmp"] / "summary.md"
    assert cli(config, "smoke", "--summary-md", str(summary)) == 0
    text = summary.read_text()
    assert "smoke test: skipped" in text and "OPENAI_API_KEY is not set" in text


def test_smoke_reports_values_and_validation_and_saves_nothing(config, offline, monkeypatch):
    palladium = csp_extraction(card_name_on_page="Bilt Palladium Card")
    use_provider(
        monkeypatch, FakeProvider({"amex-gold": gold_extraction(), "wells-fargo-bilt": palladium})
    )
    summary = config["tmp"] / "summary.md"
    before = config["details"].read_text()

    assert (
        cli(config, "smoke", "--cards", "amex-gold,wells-fargo-bilt", "--summary-md", str(summary))
        == 0
    )
    text = summary.read_text()
    assert "### American Express Gold Card: extracted and validated" in text
    assert "| field | extracted | evidence | validation |" in text
    assert "| annual_fee | $325 | “Annual Fee: $325.” | accepted |" in text
    assert "skipped (not a tracked category)" in text
    assert "| annual_fee | $250 | $325 | changed |" in text
    assert "### Bilt Mastercard: extraction rejected" in text
    assert "not used (extraction rejected)" in text
    assert list(config["data"].iterdir()) == []  # no state written
    assert config["details"].read_text() == before


def load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_extract", ROOT / "scripts" / "bootstrap_extract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_writes_a_field_by_field_comparison(config, offline, monkeypatch):
    use_provider(
        monkeypatch,
        FakeProvider(
            {
                "chase-sapphire-preferred": csp_extraction(),
                "amex-gold": gold_extraction(),
                "wells-fargo-bilt": csp_extraction(card_name_on_page="Bilt Blue Card"),
            }
        ),
    )
    out = config["tmp"] / "BOOTSTRAP_DIFF.md"
    bootstrap = load_bootstrap()
    code = bootstrap.main(
        [
            "--out",
            str(out),
            "--details",
            str(config["details"]),
            "--sources",
            str(config["sources"]),
        ]
    )
    assert code == 0
    text = out.read_text()
    assert "| card | field | hand value | extracted | evidence |" in text
    assert "| American Express Gold Card | annual_fee | $250 | $325 | “Annual Fee: $325.” |" in text
    # Bootstrap doesn't fall back to hand values, so a hand-only rate shows as missing.
    assert "| Chase Sapphire Preferred | earn.streaming | 3x | – |  |" in text
    assert "Matching fields" in text and "| Chase Sapphire Preferred | earn.dining | 3x |" in text
    assert "| Citi Custom Cash Card | no usable page (manual) |" in text
    assert "| Bilt Mastercard | extraction rejected |" in text
    # Nothing applied.
    assert load_details(config["details"]) == hand_details()


def test_bootstrap_without_key_writes_a_skip_note(config, offline):
    out = config["tmp"] / "BOOTSTRAP_DIFF.md"
    code = load_bootstrap().main(
        [
            "--out",
            str(out),
            "--details",
            str(config["details"]),
            "--sources",
            str(config["sources"]),
        ]
    )
    assert code == 0
    assert out.read_text().startswith("# Bootstrap skipped")


# ---------------------------------------------------------------------------
# Rendering details
# ---------------------------------------------------------------------------


def test_cells_are_single_line_and_escaped():
    assert render.cell("a | b\n c") == "a \\| b c"
    assert render.cell("x" * 30, 10) == "xxxxxxxxx…"


def test_pr_title_lists_three_cards_then_counts():
    report = RunReport(
        mode="full",
        today=TODAY,
        provider="fake",
        model="gpt-6-luna",
        provider_note=None,
        outcomes=[],
        usage=Usage(),
        diffs=[DiffRow(f"card-{i}", "annual_fee", "$1", "$2", "changed") for i in range(5)],
    )
    names = {f"card-{i}": f"Card {i}" for i in range(5)}
    assert render.pr_title(report, names) == "card terms changed: Card 0, Card 1, Card 2 and 2 more"


def test_pr_body_stays_under_githubs_size_limit():
    rows = [
        DiffRow(
            f"card-{i}",
            f"earn.cat{i}",
            "1x",
            "2x",
            "changed",
            "quote " * 60,
            "https://example.com/p",
        )
        for i in range(3_000)
    ]
    report = RunReport(
        mode="full",
        today=TODAY,
        provider="fake",
        model="gpt-6-luna",
        provider_note=None,
        outcomes=[],
        usage=Usage(),
        diffs=rows,
    )
    body = render.pr_body(report, {})
    assert len(body) <= render.MAX_PR_BODY
    assert "more rows" in body


def test_smoke_fails_when_a_key_is_set_but_nothing_extracts(config, offline, monkeypatch, capsys):
    rejected = LLMError(
        "OpenAI rejected the API key (HTTP 401). Check the OPENAI_API_KEY secret.", fatal=True
    )
    use_provider(monkeypatch, FakeProvider({"amex-gold": rejected, "wells-fargo-bilt": rejected}))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    summary = config["tmp"] / "summary.md"
    code = cli(
        config, "smoke", "--cards", "amex-gold,wells-fargo-bilt", "--summary-md", str(summary)
    )
    assert code == 1
    assert "**LLM problem:** OpenAI rejected the API key (HTTP 401)." in summary.read_text()
    assert "::error title=card terms::OpenAI rejected the API key" in capsys.readouterr().out


def test_bootstrap_fails_visibly_on_a_rejected_key(config, offline, monkeypatch):
    rejected = LLMError("OpenAI rejected the API key (HTTP 401).", fatal=True)
    use_provider(monkeypatch, FakeProvider({card_id: rejected for card_id in sources()}))
    out = config["tmp"] / "BOOTSTRAP_DIFF.md"
    code = load_bootstrap().main(
        [
            "--out",
            str(out),
            "--details",
            str(config["details"]),
            "--sources",
            str(config["sources"]),
        ]
    )
    assert code == 1
    assert "**LLM problem:** OpenAI rejected the API key (HTTP 401)." in out.read_text()
