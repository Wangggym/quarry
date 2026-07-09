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
    with urllib.request.urlopen(gui_server.base + "/", timeout=10) as r:
        html = r.read().decode()
    assert r.status == 200 and "<title>Quarry</title>" in html


# ---------------------------------------------------------------------------
# connection info (/api/conninfo) — resolved config, password always masked
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_mask_url_variants():
    from quarry import gui
    assert gui._mask_url("postgresql://u:secret@h:5432/db") == "postgresql://u:••••@h:5432/db"
    assert gui._mask_url("redis://:secret@h:6379/1") == "redis://:••••@h:6379/1"
    # no credentials -> unchanged
    assert gui._mask_url("postgresql://localhost:5432/db") == "postgresql://localhost:5432/db"
    assert gui._mask_url("https://ep.neptune.amazonaws.com:8182") == (
        "https://ep.neptune.amazonaws.com:8182")


@pytest.mark.unit
def test_api_conninfo_resolves_and_masks(tmp_path, monkeypatch):
    """The info panel shows what quarry will actually dial — including the SSH
    tunnel and which connections.toml the entry came from — but never the password."""
    from pathlib import Path

    from quarry import gui, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://app:hunter2@db.internal:5433/shopdb"\n'
        'engine = "postgres"\nenv = "dev"\ndb = "shop"\ngroup = "acme"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\nssh_port = 2222\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace.configure_workspace(str(tmp_path))
    try:
        info = gui.api_conninfo("shop", "dev")
    finally:
        workspace.configure_workspace(None)
    assert info["key"] == "shop_dev" and info["engine"] == "postgres"
    assert info["host"] == "db.internal" and info["port"] == 5433
    assert info["database"] == "shopdb" and info["group"] == "acme"
    assert "hunter2" not in info["url"] and info["url"].startswith("postgresql://app:")
    assert info["tunnel"] == {"host": "bastion.example.com", "user": "ec2-user",
                              "port": 2222, "key": None}
    assert info["file"].endswith("connections.toml")
    assert "hunter2" not in str(info)  # nothing anywhere in the payload leaks it


@requires_db
@pytest.mark.integration
def test_conninfo_endpoint(gui_server):
    code, body = gui_server.get("/api/conninfo?db=testpg&env=test")
    assert code == 200
    assert body["key"] == "testpg" and body["engine"] == "postgres"
    assert body["database"] and body["host"]
    assert body["tunnel"] is None
    assert body["file"].endswith("connections.toml")


@requires_db
@pytest.mark.integration
def test_conninfo_unknown_connection_is_readable_error(gui_server):
    code, body = gui_server.get("/api/conninfo?db=nope&env=test")
    assert code == 400 and "error" in body
