"""GUI HTTP API tests — drive the real ThreadingHTTPServer on an ephemeral port.

Split into two layers:
  * pure-unit tests of the backend helpers (no server, no DB)
  * e2e tests through the running server (need Postgres)
"""

from __future__ import annotations

import pytest

from conftest import requires_db


# ---------------------------------------------------------------------------
# unit — backend helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_display_path_redacts_home(monkeypatch, tmp_path):
    from pathlib import Path

    from quarry import gui
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert gui._display_path(tmp_path / "ws" / "a") == "~/ws/a"
    assert gui._display_path("/opt/other") == "/opt/other"


@pytest.mark.unit
def test_is_quarry_gui_rejects_foreign_gui(monkeypatch):
    """A foreign process whose command merely ends in 'gui' must NOT be reclaimed."""
    import subprocess

    from quarry import gui

    def fake_run(cmd, **kw):
        pid = cmd[cmd.index("-p") + 1]
        table = {
            "1": "node /app/server.js gui",           # foreign -> not ours
            "2": "python -m quarry.gui",               # ours
            "3": "/usr/local/bin/qy gui --port 8765",  # ours
            "4": "make gui",                           # foreign -> not ours
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=table.get(pid, ""), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gui._is_quarry_gui(1) is False
    assert gui._is_quarry_gui(2) is True
    assert gui._is_quarry_gui(3) is True
    assert gui._is_quarry_gui(4) is False


@pytest.mark.unit
def test_health_freshness_ttl(monkeypatch):
    from quarry import gui
    monkeypatch.setattr(gui, "HEALTH_TTL_SEC", 120)
    now = 1_000_000.0
    monkeypatch.setattr(gui.time, "time", lambda: now)
    assert gui._health_fresh_enough({"ok": True, "_ts": now - 10}) is True
    assert gui._health_fresh_enough({"ok": True, "_ts": now - 200}) is False
    assert gui._health_fresh_enough({"ok": True}) is False  # legacy entry, no _ts


# ---------------------------------------------------------------------------
# e2e — through the server
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_connections_endpoint(gui_server):
    code, body = gui_server.get("/api/connections")
    assert code == 200
    dbs = [it["db"] for g in body["groups"] for it in g["items"]]
    assert "testpg" in dbs


@requires_db
@pytest.mark.integration
def test_tables_cache_lifecycle(gui_server):
    code, first = gui_server.get("/api/tables?db=testpg&env=test&fresh=1")
    assert code == 200 and first["_cached"] is False
    assert "customers" in first["tables"] and "orders" in first["tables"]
    code, second = gui_server.get("/api/tables?db=testpg&env=test")
    assert second["_cached"] is True  # served from cache on the second hit


@requires_db
@pytest.mark.integration
def test_query_returns_typed_rows(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "SELECT * FROM customers"})
    assert code == 200
    assert body["rowCount"] == 3
    assert {c["name"] for c in body["columns"]} >= {"id", "name", "email"}


@requires_db
@pytest.mark.integration
def test_query_write_blocked(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "DELETE FROM customers"})
    assert code == 400 and body["code"] == 8


@requires_db
@pytest.mark.integration
def test_query_multi_statement_blocked(gui_server):
    code, body = gui_server.post(
        "/api/query", {"db": "testpg", "env": "test", "sql": "SELECT 1; DROP TABLE customers"})
    assert code == 400 and body["code"] == 8


@requires_db
@pytest.mark.integration
def test_run_saved_query_with_params(gui_server, tmp_path):
    (tmp_path / "queries" / "top.sql").write_text(
        "-- @name: top\n-- @db: testpg\n-- @param: n (int, default=5)\n"
        "SELECT name FROM customers ORDER BY id LIMIT :n\n", encoding="utf-8")
    code, body = gui_server.post("/api/run", {"name": "top", "env": "test", "params": {"n": "2"}})
    assert code == 200 and body["rowCount"] == 2


@requires_db
@pytest.mark.integration
def test_health_probe(gui_server):
    code, body = gui_server.get("/api/health?db=testpg&env=test&fresh=1")
    assert code == 200 and body == {"ok": True}       # no _ts leaked into the response
    code, cached = gui_server.get("/api/health?db=testpg&env=test&cached=1")
    assert cached["ok"] is True                        # painted from cache without probing


@requires_db
@pytest.mark.integration
def test_columns_endpoint_and_injection_sanitized(gui_server):
    code, body = gui_server.get("/api/columns?db=testpg&env=test&table=customers")
    assert code == 200 and "email" in body["columns"]
    # a malicious table name is sanitized to nothing dangerous -> empty, never an error
    code, evil = gui_server.get("/api/columns?db=testpg&env=test&table=x%27%3B%20DROP--")
    assert code == 200 and isinstance(evil["columns"], list)


@requires_db
@pytest.mark.integration
def test_inspect_rejects_non_redis(gui_server):
    code, body = gui_server.get("/api/inspect?db=testpg&env=test&key=foo")
    assert code == 400 and "redis-only" in body["error"]


@requires_db
@pytest.mark.integration
def test_query_missing_field_is_clean_400(gui_server):
    code, body = gui_server.post("/api/query", {})
    assert code == 400 and "db" in body["error"]


@requires_db
@pytest.mark.integration
def test_query_bad_maxrows_is_clean_400(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "SELECT 1", "maxRows": "abc"})
    assert code == 400 and "maxRows" in body["error"]


@requires_db
@pytest.mark.integration
def test_unknown_db_is_400(gui_server):
    code, body = gui_server.get("/api/tables?db=ghost&env=")
    assert code == 400 and "unknown db" in body["error"]


@requires_db
@pytest.mark.integration
def test_not_found_is_404(gui_server):
    code, _ = gui_server.get("/api/nope")
    assert code == 404


# ---------------------------------------------------------------------------
# e2e — origin / host gate (security). No DB needed but grouped with the server.
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_foreign_host_rejected(gui_server):
    code, body = gui_server.get("/api/connections", headers={"Host": "evil.example"})
    assert code == 403


@requires_db
@pytest.mark.integration
def test_foreign_origin_rejected(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "sql": "SELECT 1"},
                                 headers={"Origin": "http://evil.example"})
    assert code == 403


@requires_db
@pytest.mark.integration
def test_index_html_served(gui_server):
    import urllib.request
    req = urllib.request.Request(gui_server.base + "/")
    with gui_server._no_proxy_opener().open(req, timeout=10) as r:  # localhost: never via a proxy
        html = r.read().decode()
    assert r.status == 200 and "<title>Quarry</title>" in html
