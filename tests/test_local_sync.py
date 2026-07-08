"""Unit tests for `qy local sync` — version checks, safety gate, subprocess seams.

Real schema copy + information_schema assertions live in test_local_sync_docker.py.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import pytest

from quarry import cli, core, local, local_sync


# ---------------------------------------------------------------------------
# version parsing / compatibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,major", [
    ("pg_dump (PostgreSQL) 16.2", 16),
    ("PostgreSQL 13.15", 13),
    ("server_version\n15.4", 15),
])
def test_parse_pg_major(text, major):
    assert local_sync.parse_pg_major(text) == major


def test_parse_pg_major_invalid():
    with pytest.raises(core.QuarryError) as ei:
        local_sync.parse_pg_major("not a version")
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR


def test_assert_pg_dump_compatible_ok(monkeypatch):
    monkeypatch.setattr(local_sync, "pg_dump_major_version", lambda *a, **kw: 16)
    local_sync.assert_pg_dump_compatible(16)


def test_assert_pg_dump_compatible_too_old(monkeypatch):
    monkeypatch.setattr(local_sync, "pg_dump_major_version", lambda *a, **kw: 13)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.assert_pg_dump_compatible(16)
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "pg_dump client is PostgreSQL 13" in str(ei.value)
    assert "server is 16" in str(ei.value)


def test_pg_dump_major_version_from_mocked_subprocess(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "pg_dump (PostgreSQL) 14.1"
        stderr = ""

    monkeypatch.setattr(local_sync.subprocess, "run", lambda *a, **k: Proc())
    assert local_sync.pg_dump_major_version("/usr/bin/pg_dump") == 14


def test_server_pg_major_version(monkeypatch):
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda url, sql, **kw: (0, "16.4", ""),
    )
    assert local_sync.server_pg_major_version("postgresql://x/db") == 16


# ---------------------------------------------------------------------------
# safety gate
# ---------------------------------------------------------------------------

def _conn(**kw):
    base = core.Connection(key="shop", url="postgresql://h/db", env="dev", engine="postgres")
    return replace(base, **kw)


def test_require_local_target_accepts_local():
    local_sync._require_local_target(_conn(key="shop_local", env="local"))


def test_require_local_target_rejects_dev():
    with pytest.raises(core.QuarryError) as ei:
        local_sync._require_local_target(_conn(env="dev"))
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED
    assert "sync refused" in str(ei.value)


def test_require_local_target_rejects_prod():
    with pytest.raises(core.QuarryError) as ei:
        local_sync._require_local_target(_conn(key="shop_prod", env="prod"))
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED


def test_sync_schema_rejects_non_local_target(monkeypatch):
    def resolve(name, env=None):
        # env-set has no local member — resolve falls back to the dev connection
        return _conn(key="only_dev", env="dev")

    monkeypatch.setattr(core, "resolve_connection", resolve)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.sync_schema("only_dev", from_env="dev")
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED


def test_sync_schema_rejects_non_postgres_source(monkeypatch):
    def resolve(name, env=None):
        if env == local.LOCAL_ENV:
            return _conn(key="cache_local", env="local", engine="postgres")
        return _conn(key="cache", env="dev", engine="redis")
    monkeypatch.setattr(core, "resolve_connection", resolve)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.sync_schema("cache", from_env="dev")
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "postgres" in str(ei.value)


# ---------------------------------------------------------------------------
# orchestration seams (mocked subprocess / tunnel)
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_ws(tmp_path):
    from quarry import workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop"\n\n'
        '[shop_local]\nurl = "postgresql://localhost:5433/shop"\nengine = "postgres"\n'
        'env = "local"\ndb = "shop"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path
    workspace.configure_workspace(None)


def test_sync_schema_orchestration(monkeypatch, local_ws):
    calls: list[str] = []

    def fake_dump(url, **kw):
        calls.append(f"dump:{url}")
        return "CREATE TABLE t(id int);"

    def fake_terminate(url):
        calls.append(f"terminate:{url}")

    def fake_reset(url):
        calls.append(f"reset:{url}")

    def fake_apply(url, dump):
        calls.append(f"apply:{url}:{dump}")

    def fake_db_name(url):
        if "dev-host" in url:
            return "shop"
        return "shop"

    def fake_sanitize(dump, *, source_db, target_db):
        calls.append(f"sanitize:{source_db}->{target_db}")
        return dump

    monkeypatch.setattr(local_sync, "server_pg_major_version", lambda url: 16)
    monkeypatch.setattr(local_sync, "assert_pg_dump_compatible", lambda *a, **kw: None)
    monkeypatch.setattr(local_sync, "current_database_name", fake_db_name)
    monkeypatch.setattr(local_sync, "run_pg_dump_schema", fake_dump)
    monkeypatch.setattr(local_sync, "sanitize_schema_dump", fake_sanitize)
    monkeypatch.setattr(local_sync, "terminate_other_connections", fake_terminate)
    monkeypatch.setattr(local_sync, "reset_user_schemas", fake_reset)
    monkeypatch.setattr(local_sync, "apply_schema_dump", fake_apply)

    local_sync.sync_schema("shop", from_env="dev")
    assert calls == [
        "dump:postgresql://dev-host/shop",
        "sanitize:shop->shop",
        "terminate:postgresql://localhost:5433/shop",
        "reset:postgresql://localhost:5433/shop",
        "apply:postgresql://localhost:5433/shop:CREATE TABLE t(id int);",
    ]


def test_run_pg_dump_schema_invocation(monkeypatch):
    captured: dict = {}

    class Proc:
        returncode = 0
        stdout = "-- schema"
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(local_sync.subprocess, "run", fake_run)
    out = local_sync.run_pg_dump_schema("postgresql://u/pw@h/db", dump_bin="/bin/pg_dump")
    assert out == "-- schema"
    assert captured["cmd"] == [
        "/bin/pg_dump", "--schema-only", "--no-owner", "--no-privileges",
        "postgresql://u/pw@h/db",
    ]


def test_run_pg_dump_schema_failure(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(local_sync.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(core.QuarryError) as ei:
        local_sync.run_pg_dump_schema("postgresql://h/db", dump_bin="/bin/pg_dump")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR


def test_cmd_local_sync(monkeypatch, capsys):
    monkeypatch.setattr(local_sync, "sync_schema", lambda key, from_env="dev": None)
    args = argparse.Namespace(key="shop", from_env="dev")
    assert cli.cmd_local_sync(args) == core.EXIT_OK
    assert "synced schema" in capsys.readouterr().out


def test_fetch_schema_columns_parses_psql_output(monkeypatch):
    body = "public|customers|id|integer|||32|NO|int4\n"
    monkeypatch.setattr(local_sync, "run_psql_capture", lambda *a, **k: (0, body, ""))
    rows = local_sync.fetch_schema_columns("postgresql://h/db")
    assert rows == [("public", "customers", "id", "integer", "", "", "32", "NO", "int4")]


def test_assert_schemas_match_ok(monkeypatch):
    snap = [("public", "t", "id", "integer", "", "", "32", "NO", "int4")]
    monkeypatch.setattr(local_sync, "fetch_schema_columns", lambda url: snap)
    local_sync.assert_schemas_match("postgresql://a", "postgresql://b")


def test_reset_user_schemas_drops_all_user_schemas(monkeypatch):
    captured: dict = {}

    def fake_psql(url, sql, **kw):
        captured["url"] = url
        captured["sql"] = sql
        return 0, "", ""

    monkeypatch.setattr(local_sync, "run_psql_capture", fake_psql)
    local_sync.reset_user_schemas("postgresql://localhost/db")
    assert captured["url"] == "postgresql://localhost/db"
    assert "pg_subscription" in captured["sql"]
    assert "pg_publication" in captured["sql"]
    assert "pg_event_trigger" in captured["sql"]
    assert "pg_namespace" in captured["sql"]
    assert "DROP SCHEMA IF EXISTS %I CASCADE" in captured["sql"]
    assert "CREATE SCHEMA public" in captured["sql"]


def test_resolve_pg_dump_from_quarry_psql_dir(monkeypatch, tmp_path):
    from quarry import workspace

    bindir = tmp_path / "pg16" / "bin"
    bindir.mkdir(parents=True)
    dump = bindir / "pg_dump"
    dump.write_text("#!/bin/sh\n", encoding="utf-8")
    dump.chmod(0o755)
    psql = bindir / "psql"
    psql.write_text("#!/bin/sh\n", encoding="utf-8")
    psql.chmod(0o755)
    (tmp_path / "connections.toml").write_text("", encoding="utf-8")
    (tmp_path / "queries").mkdir()
    monkeypatch.setenv("QUARRY_PSQL", str(psql))
    workspace.configure_workspace(str(tmp_path))
    try:
        assert local_sync.resolve_pg_dump() == str(dump)
    finally:
        workspace.configure_workspace(None)


def test_sanitize_schema_dump_rewrites_database_name():
    dump = (
        "\\connect remote_db\n"
        "CREATE DATABASE remote_db;\n"
        "COMMENT ON DATABASE remote_db IS 'note';\n"
        "ALTER DATABASE remote_db SET timezone TO 'UTC';\n"
        "CREATE TABLE t(id int);\n"
    )
    out = local_sync.sanitize_schema_dump(
        dump, source_db="remote_db", target_db="local_db",
    )
    assert "\\connect" not in out
    assert "CREATE DATABASE" not in out
    assert 'COMMENT ON DATABASE "local_db"' in out
    assert 'ALTER DATABASE "local_db"' in out
    assert "CREATE TABLE t(id int);" in out


def test_sanitize_schema_dump_noop_when_names_match():
    dump = "CREATE TABLE t(id int);\n"
    assert local_sync.sanitize_schema_dump(
        dump, source_db="same", target_db="same",
    ) == dump


def test_current_database_name(monkeypatch):
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda url, sql, **kw: (0, "mydb\n", ""),
    )
    assert local_sync.current_database_name("postgresql://h/mydb") == "mydb"


def test_assert_schemas_match_fails(monkeypatch):
    monkeypatch.setattr(
        local_sync, "fetch_schema_columns",
        lambda url: [("public", "a", "id", "integer", "", "", "32", "NO", "int4")]
        if "a" in url else [],
    )
    with pytest.raises(AssertionError, match="schema mismatch"):
        local_sync.assert_schemas_match("postgresql://a", "postgresql://b")
