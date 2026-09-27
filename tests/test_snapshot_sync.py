import json

import httpx
import pytest

from card_agent.config import Settings
from card_agent.models import ChangeSet, Snapshot
from card_agent.snapshot import api_url, load_changes, load_snapshot, raw_url, sync
from tests.conftest import NOW


def _files():
    snapshot = Snapshot(generated_at=NOW, changes_file="data/changes/2026-09-26.json")
    return snapshot.model_dump_json(), ChangeSet(date=NOW.date()).model_dump_json()


def test_public_repo_uses_raw_urls(tmp_path):
    latest, changes = _files()
    settings = Settings(db_path=tmp_path / "state.db")
    routes = {
        raw_url(settings, "data/latest.json"): latest,
        raw_url(settings, "data/changes/2026-09-26.json"): changes,
    }
    seen = []

    def handler(request):
        seen.append(request)
        body = routes.get(str(request.url))
        return httpx.Response(200, text=body) if body else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = sync(settings, client)
    assert summary["origin"] == "KevinChunye/credit_card_deal_agent@data"
    assert "Authorization" not in seen[0].headers
    assert load_snapshot(settings).changes_file == "data/changes/2026-09-26.json"
    assert load_changes(settings) is not None


def test_private_repo_uses_token_and_contents_api(tmp_path):
    latest, changes = _files()
    settings = Settings(db_path=tmp_path / "state.db", github_token="ghp_test")
    routes = {
        api_url(settings, "data/latest.json"): latest,
        api_url(settings, "data/changes/2026-09-26.json"): changes,
    }
    seen = []

    def handler(request):
        seen.append(request)
        body = routes.get(str(request.url))
        return httpx.Response(200, text=body) if body else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sync(settings, client)
    assert seen[0].headers["Authorization"] == "Bearer ghp_test"
    assert seen[0].headers["Accept"] == "application/vnd.github.raw+json"


def test_bad_download_does_not_clobber_cache(tmp_path):
    latest, changes = _files()
    settings = Settings(db_path=tmp_path / "state.db")
    settings.cache_dir.mkdir(parents=True)
    (settings.cache_dir / "latest.json").write_text(latest)

    def handler(request):
        return httpx.Response(200, text=json.dumps({"not": "a snapshot"}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError):
        sync(settings, client)
    assert load_snapshot(settings).generated_at == NOW
