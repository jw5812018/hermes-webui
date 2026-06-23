"""Regression coverage for issue #4766: `/api/sessions` filters by active sidebar source."""

import io
import json
from pathlib import Path
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _session_rows(
    webui_count,
    cli_count,
    archived_webui_count=0,
    archived_cli_count=0,
    start=0,
):
    rows = []
    for index in range(webui_count):
        rows.append(
            {
                "session_id": f"webui-{start + index}",
                "title": "WebUI Session",
                "profile": "default",
                "archived": index < archived_webui_count,
                "message_count": 1,
                "updated_at": 1000 + index,
                "last_message_at": 1000 + index,
                "source": "webui",
                "raw_source": "webui",
                "session_source": "webui",
                "source_tag": "webui",
            }
        )
    for index in range(cli_count):
        rows.append(
            {
                "session_id": f"cli-{start + index + 10000}",
                "title": "Imported CLI session",
                "profile": "default",
                "archived": index < archived_cli_count,
                "message_count": 1,
                "updated_at": 2000 + index,
                "last_message_at": 2000 + index,
                "source": "cli",
                "raw_source": "cli",
                "session_source": "cli",
                "source_tag": "cli",
            }
        )
    return rows


def _handle_sessions(url):
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


@pytest.fixture(autouse=True)
def _clear_cache():
    routes._session_list_cache_clear()
    yield
    routes._session_list_cache_clear()


def _install_common_monkeypatches(monkeypatch, rows):
    enriched = []
    row_ids = {str(row["session_id"]) for row in rows if row.get("session_id")}
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: enriched.append([r["session_id"] for r in rows]))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: set(row_ids & {str(sid) for sid in ids}))
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return enriched


def test_sidebar_source_webui_excludes_cli_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    enriched = _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 30
    assert all(r["session_id"].startswith("webui-") for r in body["sessions"])
    assert body["webui_session_count"] == 30
    assert body["cli_session_count"] == 20
    assert body["archived_count"] == 0
    expected = {
        row["session_id"] for row in rows
        if not row["archived"] and row["session_id"].startswith("webui-")
    }
    assert set(enriched[0]) == expected


def test_sidebar_source_cli_excludes_webui_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=cli")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 20
    assert all(r["session_id"].startswith("cli-") for r in body["sessions"])
    assert body["webui_session_count"] == 30
    assert body["cli_session_count"] == 20


def test_sidebar_source_omitted_returns_all_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 50
    assert len([r for r in body["sessions"] if r["session_id"].startswith("webui-")]) == 30
    assert len([r for r in body["sessions"] if r["session_id"].startswith("cli-")]) == 20


def test_sidebar_source_returns_cross_bucket_counts(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20, archived_webui_count=2, archived_cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&include_archived=1")
    webui_rows = [r for r in rows if r["session_id"].startswith("webui-")]
    cli_rows = [r for r in rows if r["session_id"].startswith("cli-")]

    body = handler.json_body()
    assert handler.status == 200
    assert body["webui_session_count"] == len(webui_rows)
    assert body["cli_session_count"] == len(cli_rows)


def test_sidebar_source_preserves_archived_counts(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20, archived_webui_count=2, archived_cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&include_archived=1")
    body = handler.json_body()

    assert handler.status == 200
    assert body["archived_webui_count"] == 2
    assert body["archived_cli_count"] == 3
    assert body["archived_count"] == 5
    assert len([r for r in body["sessions"] if r["archived"]]) == 2


def test_sidebar_source_varies_cache_key():
    key_webui = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="webui",
    )
    key_cli = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="cli",
    )
    key_omitted = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source=None,
    )

    assert key_webui != key_cli
    assert key_webui != key_omitted
    assert key_cli != key_omitted


def test_frontend_sends_sidebar_source_param():
    src = SESSIONS_JS.read_text(encoding="utf-8")

    assert "const requestSidebarSource = window._showCliSessions ? _sessionSourceFilter : 'webui';" in src
    assert "qs.set('sidebar_source', requestSidebarSource);" in src
    assert "_serverWebuiSessionCount" in src
    assert "_serverCliSessionCount" in src
    assert "Number.isFinite(_serverWebuiSessionCount)" in src
    assert "Number.isFinite(_serverCliSessionCount)" in src
    assert "_sessionSourceLabel(filter,count)" in src


def test_frontend_avoids_cli_bucket_request_when_cli_hidden():
    src = SESSIONS_JS.read_text(encoding="utf-8")

    assert "window._showCliSessions ? _sessionSourceFilter : 'webui'" in src


def test_payload_row_count_regression(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")
    body = handler.json_body()

    assert handler.status == 200
    assert len(body["sessions"]) == 30
