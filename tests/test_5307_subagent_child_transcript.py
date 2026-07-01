"""Regression tests for issue #5307 — delegated subagent child transcript load.

A delegated ``delegate_task`` child is recorded in Hermes ``state.db`` with
``source='subagent'`` and usually has **no WebUI JSON sidecar** (it ran
server-side; its transcript lives only in state.db). But its ``session_id`` is
registered in the WebUI ``_index.json`` sharing the parent's lineage, often as
a ``webui``/``fork``/blank-source row.

Before the fix, ``GET /api/session`` -> ``get_session()`` raised ``KeyError``
-> ``_claim_or_synthesize_cli_session()`` saw ``_session_index_marks_was_webui``
True and returned ``"was_webui"`` -> **404**, so the child pane opened empty even
though state.db held messages.

The fix excludes delegated subagent children from the ``was_webui`` 404 gate
(``_is_subagent_child_session_id``) so they recover their state.db transcript,
while genuinely-deleted WebUI sessions keep the #2782 self-heal 404 contract.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"
SESSIONS_JS = ROOT / "static" / "sessions.js"


# ---------------------------------------------------------------------------
# Local seed helpers (mirror test_chat_start_claim_cli_session, kept
# self-contained so this file has no cross-test import dependency)
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, sid: str, *, message_count: int = 2,
                   title: str = "tui session", model: str = "MiniMax-M3",
                   source: str = "tui", cwd: str = "/root") -> None:
    """Create a minimal state.db with one session and a few messages.

    Schema mirrors hermes_state.SessionDB closely enough for
    get_state_db_session_messages to return rows.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER);
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT,
            cwd TEXT,
            rewind_count INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_call_count INTEGER DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, source, model, message_count, started_at, title, cwd) "
        "VALUES (?, ?, ?, ?, 1781024055.0, ?, ?)",
        (sid, source, model, message_count, title, cwd),
    )
    for i in range(message_count):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if i % 2 == 0 else "assistant",
             f"msg {i}", 1781024055.0 + i),
        )
    conn.commit()
    conn.close()


def _write_index(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def isolated_state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    state_dir = tmp_path / "webui-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    index_path = sessions_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")
    import api.routes as _routes
    import api.models as _models
    monkeypatch.setattr(_models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(_routes, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    return {"db": db, "state_dir": state_dir, "sessions_dir": sessions_dir,
            "index_path": index_path}


# ---------------------------------------------------------------------------
# Static checks: the fix is present in source
# ---------------------------------------------------------------------------


def test_subagent_child_helpers_defined_in_routes():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "def _is_subagent_child_session_id(" in src, (
        "routes.py must define _is_subagent_child_session_id to distinguish "
        "delegated subagent children from deleted WebUI sessions (#5307)"
    )
    assert "def _state_db_session_source(" in src, (
        "routes.py must define _state_db_session_source (cheap state.db source lookup)"
    )


def test_was_webui_gate_excludes_subagent_children():
    """The was_webui 404 gate must be guarded by
    ``not _is_subagent_child_session_id(sid)`` so subagent children fall
    through to state.db transcript recovery instead of 404ing."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    start = src.index("def _claim_or_synthesize_cli_session(")
    m = re.search(r"\n(?:def |class )", src[start + 1:])
    block = src[start:(start + 1 + m.start()) if m else len(src)]
    assert "_session_index_marks_was_webui(sid)" in block
    assert re.search(
        r"_session_index_marks_was_webui\(sid\)\s+and\s+not\s+_is_subagent_child_session_id\(sid\)",
        block,
    ), "was_webui and not subagent-child must gate the same 404 return (#5307)"


def test_sessions_js_uses_wider_import_predicate():
    """All transcript-load import_cli sites must gate on
    _sessionNeedsServerImportForLoad so subagent children trigger the
    server-side merge; the poll-skip / active-refresh gating stays keyed on the
    narrower _isExternalSession."""
    js = SESSIONS_JS.read_text(encoding="utf-8")
    assert "function _isSubagentChildSession(" in js
    assert "function _sessionNeedsServerImportForLoad(" in js
    assert js.count("_sessionNeedsServerImportForLoad(") >= 4, (
        "expected the helper definition + 3 load/tap call sites"
    )


# ---------------------------------------------------------------------------
# Functional: helper recovers subagent children, preserves #2782 404
# ---------------------------------------------------------------------------


def test_subagent_child_indexed_as_webui_is_not_404_was_webui(
    routes_module, isolated_state_db
):
    """A subagent child (source='subagent' in state.db) registered in the WebUI
    index as a webui-lineage row must NOT return 'was_webui' — it must recover
    its state.db transcript (#5307)."""
    _make_state_db(
        isolated_state_db["db"], "subagent-child-1",
        source="subagent", title="delegate child", message_count=2,
    )
    _write_index(
        isolated_state_db["index_path"],
        [
            {"session_id": "subagent-child-1", "source_tag": "webui",
             "raw_source": "webui", "session_source": "webui",
             "parent_session_id": "parent-abc"},
        ],
    )

    sess, reason = routes_module._claim_or_synthesize_cli_session("subagent-child-1")
    assert reason != "was_webui", (
        f"subagent child must not 404 as a deleted WebUI session; got reason={reason!r}"
    )


def test_deleted_webui_session_still_returns_was_webui(
    routes_module, isolated_state_db
):
    """#2782 self-heal contract preserved: a genuinely-deleted WebUI session
    (webui-origin index row, NO state.db row) still returns 'was_webui'."""
    _make_state_db(isolated_state_db["db"], "some-other-sid", source="tui")
    _write_index(
        isolated_state_db["index_path"],
        [
            {"session_id": "webui-orphan", "source_tag": "webui",
             "raw_source": "webui", "session_source": "webui"},
        ],
    )

    sess, reason = routes_module._claim_or_synthesize_cli_session("webui-orphan")
    assert sess is None
    assert reason == "was_webui", (
        "a deleted WebUI session with no state.db row must keep the #2782 404"
    )


def test_state_db_source_helper_reads_subagent(routes_module, isolated_state_db):
    """_state_db_session_source returns the lowercased source; _is_subagent_child
    is True only for source='subagent', and False for a missing row."""
    _make_state_db(
        isolated_state_db["db"], "sa-1", source="subagent", message_count=1,
    )
    assert routes_module._state_db_session_source("sa-1") == "subagent"
    assert routes_module._is_subagent_child_session_id("sa-1") is True
    assert routes_module._is_subagent_child_session_id("does-not-exist") is False
