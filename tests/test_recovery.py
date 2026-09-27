"""Failure detection and recovery: the card-data download retries, switches
endpoints, keeps the saved copy, and says what happened; typos in card names
are recovered or turned into a question. Failures are injected with
httpx.MockTransport; nothing touches the network."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from card_agent import cli, snapshot
from card_agent.config import Settings
from card_agent.matching import CardMatcher
from card_agent.models import ChangeSet, Snapshot
from card_agent.snapshot import (
    FetchFailed,
    SnapshotMissing,
    api_url,
    fetch_file,
    load_snapshot,
    raw_url,
    refresh,
)
from tests.conftest import NOW, run, setup_profile

CHANGES = "data/changes/2026-09-26.json"


def _files() -> tuple[str, str]:
    latest = Snapshot(generated_at=NOW, changes_file=CHANGES).model_dump_json()
    return latest, ChangeSet(date=NOW.date()).model_dump_json()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _no_sleep(seconds: float) -> None:
    pass


def test_retries_a_transient_error_then_succeeds(tmp_path):
    settings = Settings(db_path=tmp_path / "state.db")
    latest, _ = _files()
    calls = []
    pauses = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text=latest)

    attempts: list[dict] = []
    body = fetch_file(_client(handler), settings, "data/latest.json", attempts, pauses.append)
    assert body.decode() == latest
    assert attempts == [
        {"endpoint": "raw.githubusercontent.com", "try": 1, "error": "ConnectError"}
    ]
    assert calls == [raw_url(settings, "data/latest.json")] * 2
    assert pauses == [snapshot.BACKOFF_SECONDS]


def test_switches_to_the_api_when_raw_keeps_failing(tmp_path):
    settings = Settings(db_path=tmp_path / "state.db")
    latest, changes = _files()

    def handler(request):
        url = str(request.url)
        if url.startswith("https://raw.githubusercontent.com/"):
            return httpx.Response(503)
        routes = {
            api_url(settings, "data/latest.json"): latest,
            api_url(settings, CHANGES): changes,
        }
        return httpx.Response(200, text=routes[url])

    result = refresh(settings, NOW, client=_client(handler), sleep=_no_sleep)
    assert result.ok and result.summary["cards"] == 0
    # Two raw tries per file, then the contents API answered.
    assert [a["endpoint"] for a in result.attempts] == ["raw.githubusercontent.com"] * 4
    assert {a["error"] for a in result.attempts} == {"HTTP 503"}
    assert load_snapshot(settings).generated_at == NOW


def test_a_404_on_the_first_endpoint_is_missing_not_retried(tmp_path):
    settings = Settings(db_path=tmp_path / "state.db")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    with pytest.raises(SnapshotMissing):
        fetch_file(_client(handler), settings, "data/latest.json", sleep=_no_sleep)
    assert len(calls) == 1


def test_a_forbidden_endpoint_is_not_retried(tmp_path):
    settings = Settings(db_path=tmp_path / "state.db")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503 if "raw." in str(request.url) else 403)

    with pytest.raises(FetchFailed) as caught:
        fetch_file(_client(handler), settings, "data/latest.json", sleep=_no_sleep)
    assert [a["error"] for a in caught.value.attempts] == ["HTTP 503", "HTTP 503", "HTTP 403"]
    assert len(calls) == 3


def test_refresh_keeps_the_saved_copy_when_everything_fails(tmp_path):
    settings = Settings(db_path=tmp_path / "state.db")
    latest, _ = _files()
    settings.cache_dir.mkdir(parents=True)
    (settings.cache_dir / "latest.json").write_text(latest)

    def handler(request):
        raise httpx.ConnectError("network down")

    result = refresh(settings, NOW, client=_client(handler), sleep=_no_sleep)
    assert not result.ok and result.usable
    assert result.reason == "ConnectError" and len(result.attempts) == 4
    assert result.saved_age_days == pytest.approx(0)
    assert result.to_dict()["status"] == "saved_copy"
    assert load_snapshot(settings).generated_at == NOW  # untouched


def test_refresh_without_a_saved_copy_is_not_usable(tmp_path):
    settings = Settings(db_path=tmp_path / "state.db")

    def handler(request):
        raise httpx.ReadTimeout("slow")

    result = refresh(settings, NOW, client=_client(handler), sleep=_no_sleep)
    assert not result.usable and result.to_dict()["status"] == "failed"


def _network_down(monkeypatch):
    """Every download fails, as when the container loses its network."""

    def handler(request):
        raise httpx.ConnectError("network down")

    def failing_refresh(settings, now):
        return refresh(settings, now, client=_client(handler), sleep=_no_sleep)

    monkeypatch.setattr(cli, "refresh", failing_refresh)


def test_sync_command_recovers_with_the_saved_copy(capsys, env, monkeypatch):
    setup_profile(capsys, env)
    _network_down(monkeypatch)
    code, out = run(capsys, "sync")
    assert code == 0 and out["sync"]["status"] == "saved_copy"
    assert out["display_text"].startswith("⚠️ I couldn't reach the card data server (no connection")
    assert "still using the saved copy" in out["display_text"]
    assert out["next"] == {"action": "stop"}


def test_sync_command_escalates_without_a_saved_copy(capsys, env, monkeypatch):
    _network_down(monkeypatch)
    code, out = run(capsys, "sync")
    assert code == 1
    assert out["next"] == {"action": "ask_user", "reason": "card data unavailable"}


def test_advise_carries_on_when_a_stale_refresh_fails(capsys, env, monkeypatch):
    setup_profile(capsys, env)
    _network_down(monkeypatch)
    code, out = run(capsys, "advise", now=NOW + timedelta(days=10))
    assert code == 0 and out["status"] == "pick"
    code, trace = run(capsys, "trace", "--last", "1")
    texts = [step["text"] for step in trace["records"][-1]["steps"]]
    assert "Card data is 10 days old, so refresh it." in texts
    assert any(text.startswith("Refresh failed after 4 tries (no connection)") for text in texts)
    assert "⚠️ I couldn't refresh the card data just now" in out["display_text"]


@pytest.fixture
def matcher(env, capsys) -> CardMatcher:
    run(capsys, "sync", "--from-file", str(env.latest))
    return CardMatcher(load_snapshot(Settings.from_env()).cards)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("saphire reserve", "chase-sapphire-reserve"),
        ("chase sapphire preferd", "chase-sapphire-preferred"),
        ("ventur x", "capital-one-venture-x"),
        ("platnum", None),  # one word: never guessed
        ("zzzz qqqq", None),
    ],
)
def test_confident_guesses(matcher, query, expected):
    assert matcher.confident_guess(query) == expected


def test_unknown_names_get_suggestions(capsys, env):
    setup_profile(capsys, env)
    code, out = run(capsys, "explain", "platnum")
    assert code == 1 and out["next"] == {"action": "ask_user", "reason": "unknown card name"}
    assert out["error"].startswith("🙋 I don't know a card called 'platnum'. Did you mean")
    assert "Amex Platinum" in out["error"]


@pytest.mark.parametrize(
    ("reason", "plain"),
    [
        ("ConnectError", "no connection"),
        ("HTTP 503", "the server answered 503"),
        ("not found", "the data file wasn't found"),
        ("OSError", "an unexpected error"),
    ],
)
def test_failure_reasons_read_plainly(reason, plain):
    assert snapshot.Refresh(ok=False, reason=reason).plain_reason == plain
