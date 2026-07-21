"""Coverage-closing tests for quarry.cli — the non-Postgres engine branches.

test_cli_integration.py already exercises every command against the real local
Postgres. This file targets the branches that need a *mocked* engine (redis /
mysql / neptune have no live server here) plus a couple of error/abort paths:

  - _connections_test   : redis / mysql / neptune / psql-fail / QuarryError /
                          generic-Exception branches (cli.py 152-185)
  - cmd_describe_table   : mysql json + text render, and the psql text-path
                          timeout / failure handlers (cli.py 304-337)
  - _execute            : the prod-write abort path (cli.py 394)

Everything here is engine-mocked (no live redis/mysql/neptune), so the whole
file is @pytest.mark.unit. tunnel.open_tunnel is replaced with a trivial
contextmanager yielding a dummy URL so no real network/tunnel is touched.

Note on patch targets: cli.py imports run_mysql_query / run_neptune_cypher /
run_psql_capture *by name* from core, so those must be patched on the `cli`
module (patching core.* would not rebind the name the handler calls). redis is
reached via `redis_engine.run_redis` (attribute access), so it's patched on the
redis_engine module. Every test drives through run_cli() so the process-wide
workspace is pointed at the temp `wsdir` (isolating it from the real config.toml
aggregation) exactly as main() does.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from quarry import cli, core, redis_engine, tunnel, workspace
from quarry.core import (
    EXIT_CONNECTION_ERROR,
    EXIT_OK,
    EXIT_SQL_ERROR,
    EXIT_USAGE,
)


# ---------------------------------------------------------------------------
# main()-equivalent dispatcher (mirrors test_cli_integration.run_cli):
# build the real parser, configure the workspace, dispatch to args.func.
# ---------------------------------------------------------------------------

def run_cli(wsdir, *argv):
    args = cli.build_parser().parse_args(["--workspace", str(wsdir), *argv])
    workspace.configure_workspace(args.workspace)
    try:
        return args.func(args)
    except cli.QuarryError as e:
        return e.exit_code
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def _fake_tunnel(url: str = "engine://dummy/host"):
    """A drop-in for tunnel.open_tunnel: a contextmanager yielding a fixed URL."""
    @contextlib.contextmanager
    def _cm(conn, engine):
        yield url
    return _cm


def _write_conn(wsdir, key: str, url: str, engine: str, extra: str = "") -> None:
    (wsdir / "connections.toml").write_text(
        f'[{key}]\nurl = "{url}"\nengine = "{engine}"\n{extra}', encoding="utf-8")


# ===========================================================================
# _connections_test — engine branches (cli.py 152-185)
# ===========================================================================

@pytest.mark.unit
class TestConnectionsTestEngines:
    def test_redis_ok_reports_key_count(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        calls: list[str] = []

        def fake_run_redis(url, command, *, timeout=30):
            calls.append(command)
            if command == "PING":
                return [{"value": "PONG"}]
            return [{"value": "12"}]  # DBSIZE

        monkeypatch.setattr(redis_engine, "run_redis", fake_run_redis)
        rc = run_cli(wsdir, "connections", "test", "cache")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to Redis" in out and "12 keys" in out
        assert calls == ["PING", "DBSIZE"]  # PING first, then DBSIZE

    def test_redis_lowercase_pong_still_ok(self, wsdir, monkeypatch, capsys):
        # the PONG check upper()s the value, so a lowercase 'pong' still passes
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: [{"value": "pong"}] if command == "PING"
            else [{"value": "7"}])
        assert run_cli(wsdir, "connections", "test", "cache") == EXIT_OK
        assert "7 keys" in capsys.readouterr().out

    def test_redis_dbsize_empty_prints_question_mark(self, wsdir, monkeypatch, capsys):
        # DBSIZE returning [] exercises the `size[0]... if size else '?'` else-branch
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: [{"value": "PONG"}] if command == "PING" else [])
        rc = run_cli(wsdir, "connections", "test", "cache")
        assert rc == EXIT_OK
        assert "? keys" in capsys.readouterr().out

    def test_redis_no_pong_is_connection_error(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        # PING returns something other than PONG -> failure
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: [{"value": "NOPE"}])
        rc = run_cli(wsdir, "connections", "test", "cache")
        assert rc == EXIT_CONNECTION_ERROR
        assert "no PONG" in capsys.readouterr().err

    def test_redis_empty_ping_rows_is_connection_error(self, wsdir, monkeypatch, capsys):
        # PING returning [] makes `rows and ...` falsy -> the not-ok branch
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(redis_engine, "run_redis", lambda url, command, *, timeout=30: [])
        assert run_cli(wsdir, "connections", "test", "cache") == EXIT_CONNECTION_ERROR

    def test_neptune_ok(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "graph", "https://neptune.example.com:8182", "neptune")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_cypher(url, cypher, *, timeout=30, **kwargs):
            seen["cypher"] = cypher
            return [{"ok": 1}]

        # cli.py calls the name imported into its own namespace
        monkeypatch.setattr(cli, "run_neptune_cypher", fake_cypher)
        rc = run_cli(wsdir, "connections", "test", "graph")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to Neptune" in out
        assert seen["cypher"] == "RETURN 1 AS ok"

    def test_mysql_ok_prints_db_and_version(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_mysql_query",
            lambda url, sql, *, timeout=30: [{"db_name": "shopdb", "version": "8.0.34"}])
        rc = run_cli(wsdir, "connections", "test", "shop")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to shopdb (mysql)" in out
        assert "8.0.34" in out  # the version sub-line

    def test_mysql_ok_no_version_and_no_rows(self, wsdir, monkeypatch, capsys):
        # empty result -> row={} -> '?' for db_name and the version sub-line is skipped
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(cli, "run_mysql_query", lambda url, sql, *, timeout=30: [])
        rc = run_cli(wsdir, "connections", "test", "shop")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to ? (mysql)" in out
        assert "8.0" not in out  # no version line emitted

    def test_postgres_psql_capture_failure(self, wsdir, monkeypatch, capsys):
        # engine=postgres path: run_psql_capture returns nonzero -> connection error
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (1, "", "FATAL: nope"))
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_CONNECTION_ERROR
        assert "connection test failed" in capsys.readouterr().err

    def test_postgres_psql_capture_ok_single_part(self, wsdir, monkeypatch, capsys):
        # a one-field psql result: parts has length 1 so the version sub-line
        # (len(parts) > 1) is skipped — covers the 177->179 false branch.
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (0, "mydb", ""))
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to mydb" in out

    def test_postgres_psql_capture_ok_with_version(self, wsdir, monkeypatch, capsys):
        # a two-part psql result ('db | version') -> the version sub-line at 178
        # (len(parts) > 1 true branch) is printed.
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (0, "mydb | PostgreSQL 15.2 on x86_64", ""))
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to mydb" in out
        assert "PostgreSQL 15.2" in out  # the version sub-line

    def test_open_tunnel_quarry_error_returns_its_exit_code(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")

        @contextlib.contextmanager
        def boom(conn, engine):
            raise core.QuarryError("tunnel down", exit_code=EXIT_USAGE)
            yield  # pragma: no cover

        monkeypatch.setattr(tunnel, "open_tunnel", boom)
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_USAGE  # the QuarryError's own exit_code, not the generic one
        assert "connection test failed: tunnel down" in capsys.readouterr().err

    def test_open_tunnel_generic_exception_is_connection_error(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")

        @contextlib.contextmanager
        def boom(conn, engine):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

        monkeypatch.setattr(tunnel, "open_tunnel", boom)
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_CONNECTION_ERROR
        assert "connection test failed: kaboom" in capsys.readouterr().err


# ===========================================================================
# cmd_describe_table — mysql render + psql text-path error handlers
# (cli.py 304-337)
# ===========================================================================

_MYSQL_COLS = [
    {"column_name": "id", "data_type": "int", "is_nullable": "NO", "column_default": None},
    {"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "column_default": "''"},
]


@pytest.mark.unit
class TestDescribeTableMysql:
    def test_mysql_text_render(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_mysql_query",
            lambda url, sql, *, params=None, timeout=15: list(_MYSQL_COLS))
        rc = run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "text")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # header row + separator + both columns rendered
        assert "column_name" in out and "data_type" in out
        assert "id" in out and "name" in out
        assert "----" in out  # the dashed separator line

    def test_mysql_text_empty_table(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_mysql_query", lambda url, sql, *, params=None, timeout=15: [])
        rc = run_cli(wsdir, "describe-table", "shop", "ghost", "--format", "text")
        assert rc == EXIT_OK
        assert "not found or has no columns" in capsys.readouterr().out

    def test_mysql_json_render(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_mysql_query",
            lambda url, sql, *, params=None, timeout=15: list(_MYSQL_COLS))
        rc = run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["table"] == "widgets"
        assert [c["column_name"] for c in obj["columns"]] == ["id", "name"]

    def test_mysql_query_error_maps_to_sql_error(self, wsdir, monkeypatch):
        # run_mysql_query raising -> the except handler err()s with EXIT_SQL_ERROR
        # (which raises QuarryError; run_cli maps it to the exit code — the message
        # rides on the exception rather than stderr).
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def boom(url, sql, *, params=None, timeout=15):
            raise RuntimeError("mysql exploded")

        monkeypatch.setattr(cli, "run_mysql_query", boom)
        args = cli.build_parser().parse_args(
            ["--workspace", str(wsdir), "describe-table", "shop", "widgets", "--format", "json"])
        workspace.configure_workspace(args.workspace)
        with pytest.raises(core.QuarryError) as ei:
            args.func(args)
        assert ei.value.exit_code == EXIT_SQL_ERROR
        assert "mysql failed" in str(ei.value)


@pytest.mark.unit
class TestDescribeTableUnsupportedEngines:
    @pytest.mark.parametrize("engine,url", [
        ("redis", "redis://localhost:6379/0"),
        ("neptune", "https://neptune.example.com:8182"),
    ])
    def test_unsupported_engine_is_usage_error(self, wsdir, engine, url):
        # redis/neptune reject describe-table before any tunnel access via
        # err(..., EXIT_USAGE), which raises QuarryError carrying the message.
        _write_conn(wsdir, "c", url, engine)
        args = cli.build_parser().parse_args(
            ["--workspace", str(wsdir), "describe-table", "c", "t"])
        workspace.configure_workspace(args.workspace)
        with pytest.raises(core.QuarryError) as ei:
            args.func(args)
        assert ei.value.exit_code == EXIT_USAGE
        assert f"not supported for engine={engine}" in str(ei.value)


@pytest.mark.unit
class TestDescribeTablePsqlTextErrors:
    """The engine=postgres text path shells out to psql; cover its timeout and
    non-zero-exit handlers with a mocked subprocess (no real psql invoked).
    err(..., exit_code=...) raises QuarryError, so run_cli reports the exit code
    (the message rides on the exception, not stderr)."""

    def test_psql_text_timeout(self, wsdir, monkeypatch):
        import subprocess as _sp
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_run(cmd, **kw):
            raise _sp.TimeoutExpired(cmd, kw.get("timeout", 15))

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = run_cli(wsdir, "describe-table", "pg", "customers", "--format", "text")
        assert rc == EXIT_CONNECTION_ERROR

    def test_psql_text_nonzero_exit(self, wsdir, monkeypatch):
        import subprocess as _sp
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_run(cmd, **kw):
            return _sp.CompletedProcess(cmd, returncode=1, stdout="", stderr="psql: boom")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = run_cli(wsdir, "describe-table", "pg", "customers", "--format", "text")
        assert rc == EXIT_SQL_ERROR

    def test_psql_text_ok(self, wsdir, monkeypatch, capsys):
        import subprocess as _sp
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_run(cmd, **kw):
            return _sp.CompletedProcess(
                cmd, returncode=0, stdout="Table widgets\n id | int\n", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = run_cli(wsdir, "describe-table", "pg", "widgets", "--format", "text")
        assert rc == EXIT_OK
        assert "Table widgets" in capsys.readouterr().out


# ===========================================================================
# _execute — prod-write abort path (cli.py 394 / the confirm-abort guard)
# ===========================================================================

@pytest.mark.unit
class TestExecuteProdAbort:
    def test_prod_write_declined_aborts(self, wsdir, monkeypatch):
        # A write against a prod connection with --write but not --yes prompts; a
        # 'n' answer makes _confirm_prod_write return False, so _execute calls
        # err('aborted', EXIT_USAGE) — which raises QuarryError (err with an
        # exit_code raises, it does not return) — and execute_sql is never reached.
        import argparse
        conn = core.Connection(key="prodpg", url="postgresql://localhost/x", env="prod")

        called = {"execute": False}
        monkeypatch.setattr(
            core, "execute_sql",
            lambda **kw: called.__setitem__("execute", True) or EXIT_OK)  # pragma: no cover
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("n\n"))

        args = argparse.Namespace(write=True, yes=False, format="json", max_rows=None)
        with pytest.raises(core.QuarryError) as ei:
            cli._execute(conn, "DELETE FROM t", {}, args)
        assert ei.value.exit_code == EXIT_USAGE
        assert "aborted" in str(ei.value)
        assert called["execute"] is False  # write blocked before reaching the engine
