"""GUI backend helper + branch tests (quarry.gui).

Companion to test_gui_api.py, which drives the endpoints through the live
ThreadingHTTPServer. This file exercises the *internal helpers and branches*
that HTTP-level tests miss — cache persistence, the per-engine branches of
api_health / api_tables / api_columns / api_inspect / _list_tables, and the
port-reclaim / _bind logic — almost entirely with mocks (no server, no DB
except a couple of @requires_db in-process integration checks).

We NEVER call serve() (it blocks) or webbrowser.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import socket
import subprocess

import pytest

from conftest import requires_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Conn:
    """Minimal stand-in for a core.Connection (only .url is read by tunnel/mocks)."""

    def __init__(self, url="postgresql://localhost/x", ssh_host=None):
        self.url = url
        self.ssh_host = ssh_host


def _fake_tunnel(expected_url="URL"):
    """Return a function usable as a monkeypatched tunnel.open_tunnel: a
    @contextmanager that yields a fixed effective URL."""

    @contextlib.contextmanager
    def _open(conn, engine):
        yield expected_url

    return _open


class _Res:
    """Stand-in for core.QueryResult with just the .rows attribute _list_tables /
    api_columns read."""

    def __init__(self, rows):
        self.rows = rows


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point gui._CACHE_FILE at a tmp file and start from an empty in-memory cache,
    so cache tests never touch ~/.cache and are order-independent."""
    from quarry import gui

    monkeypatch.setattr(gui, "_CACHE_FILE", tmp_path / "gui-cache.json")
    gui._CACHE.clear()
    yield gui
    gui._CACHE.clear()


# ---------------------------------------------------------------------------
# cache: put -> save -> load round-trip
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cache_put_persists_and_reloads(isolated_cache):
    gui = isolated_cache
    ret = gui._cache_put("k1", {"a": 1, "unicode": "héllo"})
    assert ret == {"a": 1, "unicode": "héllo"}          # returns the value
    assert gui._cache_get("k1") == {"a": 1, "unicode": "héllo"}
    assert gui._CACHE_FILE.exists()                      # _save_cache wrote JSON

    # Simulate a fresh process: drop the in-memory cache, then _load_cache reads it back.
    gui._CACHE.clear()
    assert gui._cache_get("k1") is None
    gui._load_cache()
    assert gui._cache_get("k1") == {"a": 1, "unicode": "héllo"}


@pytest.mark.unit
def test_cache_get_missing_returns_none(isolated_cache):
    assert isolated_cache._cache_get("nope") is None


@pytest.mark.unit
def test_load_cache_ignores_bad_file(isolated_cache):
    gui = isolated_cache
    gui._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    gui._CACHE_FILE.write_text("{not json", encoding="utf-8")
    gui._load_cache()                       # must not raise
    assert gui._cache_get("anything") is None


@pytest.mark.unit
def test_load_cache_ignores_non_dict_json(isolated_cache):
    gui = isolated_cache
    gui._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    gui._CACHE_FILE.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong type
    gui._load_cache()
    assert gui._cache_get("anything") is None


@pytest.mark.unit
def test_save_cache_swallows_errors(isolated_cache, monkeypatch):
    """_save_cache must never raise even if the FS write fails."""
    gui = isolated_cache

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gui.Path, "mkdir", boom)
    gui._cache_put("k", {"v": 1})           # _save_cache internally catches OSError
    assert gui._cache_get("k") == {"v": 1}  # in-memory put still succeeded


# ---------------------------------------------------------------------------
# health helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_put_health_stamps_ts_and_returns_clean(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.time, "time", lambda: 5000.0)
    out = gui._put_health("health:x@dev", {"ok": True})
    assert out == {"ok": True}                      # returned value has NO _ts
    stored = gui._cache_get("health:x@dev")
    assert stored["ok"] is True and stored["_ts"] == 5000.0   # stored value HAS _ts


@pytest.mark.unit
def test_health_fresh_enough_boundary(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 1000.0)
    assert gui._health_fresh_enough({"_ts": 1000.0}) is True          # age 0
    assert gui._health_fresh_enough({"_ts": 901.0}) is True           # age 99 < 100
    assert gui._health_fresh_enough({"_ts": 900.0}) is False          # age 100, not < 100
    assert gui._health_fresh_enough({"_ts": "bad"}) is False          # non-numeric
    assert gui._health_fresh_enough({}) is False                      # missing _ts


# ---------------------------------------------------------------------------
# api_health — each engine branch, mocked (no real DB)
# ---------------------------------------------------------------------------

def _patch_health(monkeypatch, gui, engine, conn=None):
    """Wire _resolve/connection_engine/open_tunnel for a mocked api_health probe."""
    conn = conn or _Conn()
    monkeypatch.setattr(gui, "_resolve", lambda db, env: conn)
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: engine)
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("EFF_URL"))


@pytest.mark.unit
def test_api_health_redis_ping_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "redis")
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "run_redis",
                        lambda url, cmd, timeout=6: seen.update(url=url, cmd=cmd) or [])
    out = gui.api_health("r", "dev", fresh=True)
    assert out == {"ok": True}
    assert seen == {"url": "EFF_URL", "cmd": "PING"}


@pytest.mark.unit
def test_api_health_mysql_select1_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "mysql")
    calls = []
    monkeypatch.setattr(gui.core, "run_mysql_query",
                        lambda url, sql, timeout=6: calls.append((url, sql)))
    out = gui.api_health("m", "dev", fresh=True)
    assert out == {"ok": True}
    assert calls == [("EFF_URL", "SELECT 1")]


@pytest.mark.unit
def test_api_health_neptune_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "neptune")
    calls = []
    monkeypatch.setattr(gui.core, "run_neptune_cypher",
                        lambda url, cy, timeout=6: calls.append((url, cy)))
    out = gui.api_health("n", "dev", fresh=True)
    assert out == {"ok": True}
    assert calls == [("EFF_URL", "RETURN 1 AS ok")]


@pytest.mark.unit
def test_api_health_postgres_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (0, "1", ""))
    assert gui.api_health("p", "dev", fresh=True) == {"ok": True}


@pytest.mark.unit
def test_api_health_postgres_rc_nonzero_error(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (2, "", "  FATAL: nope  "))
    out = gui.api_health("p", "dev", fresh=True)
    assert out == {"ok": False, "error": "FATAL: nope"}   # stripped


@pytest.mark.unit
def test_api_health_postgres_rc_nonzero_empty_stderr(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (1, "", "   "))
    out = gui.api_health("p", "dev", fresh=True)
    assert out == {"ok": False, "error": "connect failed"}  # fallback message


@pytest.mark.unit
def test_api_health_exception_branch(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: (_ for _ in ()).throw(RuntimeError("x" * 400)))
    out = gui.api_health("boom", "dev", fresh=True)
    assert out["ok"] is False
    assert out["error"] == "x" * 200          # truncated to 200 chars


@pytest.mark.unit
def test_api_health_cached_only_nothing_fresh(isolated_cache):
    gui = isolated_cache
    # No cache entry at all -> cached_only returns {ok: None} without probing.
    assert gui.api_health("never", "dev", cached_only=True) == {"ok": None}


@pytest.mark.unit
def test_api_health_cached_only_returns_fresh_entry(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 2000.0)
    gui._put_health("health:c@dev", {"ok": True})          # fresh (_ts=2000)
    # Even with a resolve that would blow up, cached_only must NOT probe.
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: (_ for _ in ()).throw(AssertionError("probed!")))
    assert gui.api_health("c", "dev", cached_only=True) == {"ok": True}


@pytest.mark.unit
def test_api_health_fresh_bypasses_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 3000.0)
    gui._put_health("health:p@dev", {"ok": False, "error": "old"})  # a fresh cached failure
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (0, "1", ""))
    # fresh=True ignores the fresh-but-stale-in-meaning cache and re-probes -> ok.
    assert gui.api_health("p", "dev", fresh=True) == {"ok": True}


@pytest.mark.unit
def test_api_health_uses_fresh_cache_without_probe(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 4000.0)
    gui._put_health("health:p@dev", {"ok": True})
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: (_ for _ in ()).throw(AssertionError("probed!")))
    # Not fresh, cache present + fresh_enough -> served from cache, _ts stripped.
    assert gui.api_health("p", "dev") == {"ok": True}


# ---------------------------------------------------------------------------
# api_tables — engine branches + cache put/hit
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_tables_mysql_branch_and_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    monkeypatch.setattr(gui, "_list_tables", lambda conn: ["t_a", "t_b"])

    out = gui.api_tables("m", "dev", fresh=True)
    assert out == {"tables": ["t_a", "t_b"], "engine": "mysql", "capped": False, "_cached": False}

    # Second call (fresh=False) is served from cache with _cached=True and no _list_tables.
    monkeypatch.setattr(gui, "_list_tables",
                        lambda conn: pytest.fail("should not re-list on cache hit"))
    hit = gui.api_tables("m", "dev")
    assert hit == {"tables": ["t_a", "t_b"], "engine": "mysql", "capped": False, "_cached": True}


@pytest.mark.unit
def test_api_tables_neptune_branch(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "neptune")
    # neptune _list_tables returns [] (no SQL); assert the real path via _list_tables.
    out = gui.api_tables("n", None, fresh=True)
    assert out == {"tables": [], "engine": "neptune", "capped": False, "_cached": False}


@pytest.mark.unit
def test_api_tables_redis_branch_and_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "redis")
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("REDIS_URL"))
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "keys_with_meta",
                        lambda url, cap=400: seen.update(url=url, cap=cap) or [{"key": "k1"}])
    out = gui.api_tables("r", "dev", fresh=True)
    assert out == {"engine": "redis", "keys": [{"key": "k1"}], "capped": False, "_cached": False}
    assert seen == {"url": "REDIS_URL", "cap": 400}

    # cache hit
    hit = gui.api_tables("r", "dev")
    assert hit["_cached"] is True and hit["keys"] == [{"key": "k1"}]


@pytest.mark.unit
def test_api_tables_cache_miss_when_not_fresh(isolated_cache, monkeypatch):
    """fresh=False but no cache entry yet -> falls through to a real list (branch 126->128)."""
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    monkeypatch.setattr(gui, "_list_tables", lambda conn: ["only"])
    out = gui.api_tables("m", "dev")            # fresh defaults to False, cache empty
    assert out == {"tables": ["only"], "engine": "mysql", "capped": False, "_cached": False}


@pytest.mark.unit
def test_api_tables_fresh_bypasses_existing_cache(isolated_cache, monkeypatch):
    """fresh=True must re-list even when a cache entry already exists (branch 126->128)."""
    gui = isolated_cache
    gui._cache_put("tables:m@dev", {"tables": ["stale"], "engine": "mysql"})
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    monkeypatch.setattr(gui, "_list_tables", lambda conn: ["fresh1", "fresh2"])
    out = gui.api_tables("m", "dev", fresh=True)
    assert out == {"tables": ["fresh1", "fresh2"], "engine": "mysql", "capped": False, "_cached": False}


@pytest.mark.unit
def test_api_tables_capped_flag_at_5000(isolated_cache, monkeypatch):
    """A table list that hits _list_tables' 5000-row cap is flagged so the UI
    can say 'showing only the first N tables' instead of silently truncating."""
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(gui, "_list_tables", lambda conn: [f"t{i}" for i in range(5000)])
    out = gui.api_tables("p", "dev", fresh=True)
    assert out["capped"] is True and len(out["tables"]) == 5000


# ---------------------------------------------------------------------------
# _req / _max_rows / api_query / api_run  (pure POST-handler helpers)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_req_present_and_missing(isolated_cache):
    from quarry.core import QuarryError

    gui = isolated_cache
    assert gui._req({"db": "x"}, "db") == "x"
    for body in ({}, {"db": None}, {"db": ""}):
        with pytest.raises(QuarryError, match="missing required field 'db'"):
            gui._req(body, "db")


@pytest.mark.unit
def test_max_rows_default_int_and_bad(isolated_cache):
    from quarry.core import QuarryError

    gui = isolated_cache
    assert gui._max_rows({}) == 500                 # default when absent
    assert gui._max_rows({"maxRows": 0}) == 500     # falsy 0 -> default (0 or 500)
    assert gui._max_rows({"maxRows": "25"}) == 25   # numeric string coerced
    assert gui._max_rows({"maxRows": 42}) == 42
    with pytest.raises(QuarryError, match="maxRows must be an integer"):
        gui._max_rows({"maxRows": "abc"})


@pytest.mark.unit
def test_api_query_wires_resolve_and_run_query(isolated_cache, monkeypatch):
    gui = isolated_cache
    seen = {}

    class _R:
        def to_dict(self):
            return {"rows": [{"n": 1}], "columns": []}

    def fake_resolve(db, env):
        seen["resolve"] = (db, env)
        return _Conn()

    monkeypatch.setattr(gui, "_resolve", fake_resolve)

    def fake_run(conn, sql, *, max_rows, with_types):
        seen["run"] = (sql, max_rows, with_types)
        return _R()

    monkeypatch.setattr(gui.core, "run_query", fake_run)
    out = gui.api_query({"db": "d", "env": "e", "sql": "SELECT 1", "maxRows": "7"})
    assert out == {"rows": [{"n": 1}], "columns": []}
    assert seen["resolve"] == ("d", "e")
    assert seen["run"] == ("SELECT 1", 7, True)


@pytest.mark.unit
def test_api_run_loads_named_query(isolated_cache, monkeypatch):
    gui = isolated_cache

    class _Q:
        db = "reports"
        sql = "SELECT * FROM t WHERE x = :x"

    class _R:
        def to_dict(self):
            return {"rows": [], "columns": []}

    seen = {}

    def fake_load_query(name):
        seen["name"] = name
        return _Q()

    def fake_resolve(db, env):
        seen["resolve"] = (db, env)
        return _Conn()

    def fake_resolve_params(q, p):
        seen["params"] = p
        return {"x": "1"}

    monkeypatch.setattr(gui.core, "load_query", fake_load_query)
    monkeypatch.setattr(gui, "_resolve", fake_resolve)
    monkeypatch.setattr(gui.core, "resolve_params", fake_resolve_params)

    def fake_run(conn, sql, *, params, max_rows, with_types):
        seen["run"] = (sql, params, max_rows, with_types)
        return _R()

    monkeypatch.setattr(gui.core, "run_query", fake_run)
    out = gui.api_run({"name": "rep", "env": "prod", "params": {"x": "1"}, "maxRows": 9})
    assert out == {"rows": [], "columns": []}
    assert seen["name"] == "rep"
    assert seen["resolve"] == ("reports", "prod")
    assert seen["params"] == {"x": "1"}
    assert seen["run"] == ("SELECT * FROM t WHERE x = :x", {"x": "1"}, 9, True)


# ---------------------------------------------------------------------------
# api_columns — sanitizer, caching, redis/neptune, empty
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_columns_empty_table_no_db(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: pytest.fail("must not resolve for empty table"))
    assert gui.api_columns("db", "dev", "") == {"columns": []}
    # A table that sanitizes to empty (only illegal chars) is also a no-op.
    assert gui.api_columns("db", "dev", "!!!;--") == {"columns": []}


@pytest.mark.unit
def test_api_columns_sanitizer_keeps_word_dollar_only(isolated_cache, monkeypatch):
    gui = isolated_cache
    captured = {}

    def fake_run_query(conn, sql, max_rows=2000):
        captured["sql"] = sql
        return _Res([{"column_name": "id"}, {"column_name": "name"}, {"column_name": None}])

    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(gui.core, "run_query", fake_run_query)

    # "users; DROP" -> keeps [A-Za-z0-9_$] only -> "usersDROP"
    out = gui.api_columns("db", "dev", "users; DROP TABLE x$1")
    assert out == {"columns": ["id", "name"]}            # None column filtered out
    assert "usersDROPTABLEx$1" in captured["sql"]        # sanitized name embedded
    assert ";" not in captured["sql"].split("table_name = ")[1].split("ORDER")[0]

    # mysql uses DATABASE() schema
    captured.clear()
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    gui.api_columns("db", "dev2", "orders")
    assert "DATABASE()" in captured["sql"]


@pytest.mark.unit
def test_api_columns_caches_result(isolated_cache, monkeypatch):
    gui = isolated_cache
    n = {"calls": 0}

    def fake_run_query(conn, sql, max_rows=2000):
        n["calls"] += 1
        return _Res([{"column_name": "id"}])

    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(gui.core, "run_query", fake_run_query)

    first = gui.api_columns("db", "dev", "t")
    second = gui.api_columns("db", "dev", "t")
    assert first == second == {"columns": ["id"]}
    assert n["calls"] == 1                                # second served from cache


@pytest.mark.unit
def test_api_columns_redis_neptune_return_empty_and_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    for eng in ("redis", "neptune"):
        monkeypatch.setattr(gui.core, "connection_engine", lambda c, e=eng: e)
        monkeypatch.setattr(gui.core, "run_query",
                            lambda *a, **k: pytest.fail("no DB call for redis/neptune"))
        out = gui.api_columns("db", eng, "tbl")
        assert out == {"columns": []}
        # cached: the key is stored (a later hit returns the same object, no resolve).
        assert gui._cache_get(f"columns:db@{eng}:tbl") == {"columns": []}


@pytest.mark.unit
def test_api_columns_swallows_exception(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gui.api_columns("db", "dev", "t") == {"columns": []}


# ---------------------------------------------------------------------------
# api_inspect — redis happy path + non-redis rejection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_inspect_redis_happy_path(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "redis")
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("REDIS_URL"))
    rows = [{"field": "a", "value": "1"}, {"field": "b", "value": "2"}]
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "inspect_key",
                        lambda url, key: seen.update(url=url, key=key) or rows)

    out = gui.api_inspect("r", "dev", "user:1")
    assert seen == {"url": "REDIS_URL", "key": "user:1"}
    assert out["rows"] == rows
    assert out["rowCount"] == 2
    assert out["engine"] == "redis"
    assert out["truncated"] is False
    assert out["sql"] == "# inspect user:1"
    assert out["columns"] == [{"name": "field", "type": None}, {"name": "value", "type": None}]


@pytest.mark.unit
def test_api_inspect_missing_key_raises(isolated_cache):
    from quarry.core import QuarryError

    with pytest.raises(QuarryError, match="requires a 'key'"):
        isolated_cache.api_inspect("r", "dev", "")


@pytest.mark.unit
def test_api_inspect_non_redis_raises(isolated_cache, monkeypatch):
    from quarry.core import QuarryError

    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    with pytest.raises(QuarryError, match="redis-only"):
        gui.api_inspect("p", "dev", "somekey")


# ---------------------------------------------------------------------------
# _list_tables — mysql / neptune / redis / postgres branches
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_list_tables_mysql(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    captured = {}

    def fake_run_query(conn, sql, max_rows=5000):
        captured["sql"] = sql
        return _Res([{"table_name": "t1"}, {"table_name": None}, {"table_name": "t2"}])

    monkeypatch.setattr(gui.core, "run_query", fake_run_query)
    assert gui._list_tables(_Conn()) == ["t1", "t2"]     # None filtered
    assert "DATABASE()" in captured["sql"]


@pytest.mark.unit
def test_list_tables_postgres(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    captured = {}

    def fake_run_query(conn, sql, max_rows=5000):
        captured["sql"] = sql
        return _Res([{"table_name": "customers"}])

    monkeypatch.setattr(gui.core, "run_query", fake_run_query)
    assert gui._list_tables(_Conn()) == ["customers"]
    assert "'public'" in captured["sql"]


@pytest.mark.unit
def test_list_tables_neptune_is_empty_no_query(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "neptune")
    monkeypatch.setattr(gui.core, "run_query",
                        lambda *a, **k: pytest.fail("neptune must not run a query"))
    assert gui._list_tables(_Conn()) == []


@pytest.mark.unit
def test_list_tables_redis_scans_keys(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "redis")
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("RURL"))
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "scan_keys",
                        lambda url, count=1000: seen.update(url=url, count=count) or ["k1", "k2"])
    assert gui._list_tables(_Conn()) == ["k1", "k2"]
    assert seen == {"url": "RURL", "count": 1000}


# ---------------------------------------------------------------------------
# _resolve delegates to core.resolve_connection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_resolve_delegates(isolated_cache, monkeypatch):
    gui = isolated_cache
    calls = []
    sentinel = _Conn()
    monkeypatch.setattr(gui.core, "resolve_connection",
                        lambda db, env: calls.append((db, env)) or sentinel)
    assert gui._resolve("mydb", "prod") is sentinel
    assert calls == [("mydb", "prod")]


# ---------------------------------------------------------------------------
# _display_path
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_display_path(monkeypatch, tmp_path):
    from pathlib import Path

    from quarry import gui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert gui._display_path(tmp_path / "proj" / "ws") == "~/proj/ws"
    assert gui._display_path(tmp_path) == "~"                    # exactly home
    assert gui._display_path("/somewhere/else") == "/somewhere/else"


# ---------------------------------------------------------------------------
# port management: _port_pids / _is_quarry_gui / _reclaim_port / _next_free_port
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_port_pids_parses_lsof(monkeypatch):
    from quarry import gui

    def fake_run(cmd, **kw):
        assert cmd[0] == "lsof"
        return subprocess.CompletedProcess(cmd, 0, stdout="123\n456\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gui._port_pids(8765) == [123, 456]


@pytest.mark.unit
def test_port_pids_swallows_errors(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("lsof missing")))
    assert gui._port_pids(8765) == []


@pytest.mark.unit
def test_is_quarry_gui_ours_vs_foreign(monkeypatch):
    from quarry import gui

    table = {
        "10": "python -m quarry.gui --port 8765",   # ours (-m quarry + gui)
        "11": "/usr/local/bin/qy gui",              # ours (/qy + gui)
        "12": "node server.js gui",                 # foreign (no quarry marker)
        "13": "postgres: writer process",           # foreign (no 'gui')
    }

    def fake_run(cmd, **kw):
        pid = cmd[cmd.index("-p") + 1]
        return subprocess.CompletedProcess(cmd, 0, stdout=table.get(pid, ""), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gui._is_quarry_gui(10) is True
    assert gui._is_quarry_gui(11) is True
    assert gui._is_quarry_gui(12) is False
    assert gui._is_quarry_gui(13) is False


@pytest.mark.unit
def test_is_quarry_gui_swallows_errors(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ps missing")))
    assert gui._is_quarry_gui(1) is False


@pytest.mark.unit
def test_reclaim_port_kills_only_ours(monkeypatch):
    """Only a pid that is ours AND not our own process gets SIGTERM'd."""
    from quarry import gui

    monkeypatch.setattr(gui, "_port_pids", lambda port: [111, 222, os.getpid()])
    # 111 is ours (kill), 222 is foreign (skip), our own pid is skipped by the guard.
    monkeypatch.setattr(gui, "_is_quarry_gui", lambda pid: pid == 111)
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    killed = []
    monkeypatch.setattr(gui.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert gui._reclaim_port(8765) is True
    assert killed == [(111, signal.SIGTERM)]             # ONLY 111


@pytest.mark.unit
def test_reclaim_port_nothing_to_kill(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui, "_port_pids", lambda port: [999])
    monkeypatch.setattr(gui, "_is_quarry_gui", lambda pid: False)   # all foreign
    monkeypatch.setattr(gui.os, "kill",
                        lambda *a: pytest.fail("must not kill a foreign pid"))
    assert gui._reclaim_port(8765) is False


@pytest.mark.unit
def test_reclaim_port_kill_failure_is_swallowed(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui, "_port_pids", lambda port: [111])
    monkeypatch.setattr(gui, "_is_quarry_gui", lambda pid: True)
    monkeypatch.setattr(gui.os, "kill",
                        lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    # killed stays False because os.kill raised -> no sleep, returns False.
    monkeypatch.setattr(gui.time, "sleep",
                        lambda s: pytest.fail("should not sleep when nothing killed"))
    assert gui._reclaim_port(8765) is False


@pytest.mark.unit
def test_next_free_port_finds_open(monkeypatch):
    from quarry import gui

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, addr):
            host, port = addr
            if port < 9003:                 # first two candidates "in use"
                raise OSError("in use")
            # else succeeds

    monkeypatch.setattr(gui.socket, "socket", lambda *a, **k: FakeSock())
    assert gui._next_free_port("127.0.0.1", 9000) == 9003


@pytest.mark.unit
def test_next_free_port_exhausted_returns_start(monkeypatch):
    from quarry import gui

    class AlwaysBusy:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, addr):
            raise OSError("in use")

    monkeypatch.setattr(gui.socket, "socket", lambda *a, **k: AlwaysBusy())
    assert gui._next_free_port("127.0.0.1", 8000, tries=5) == 8000


# ---------------------------------------------------------------------------
# _bind — clean bind, reclaim-and-takeover, foreign->next-free, re-raise
# ---------------------------------------------------------------------------

class _FakeServer:
    """Records the (host, port) it was constructed with."""

    instances = []

    def __init__(self, addr, handler):
        self.addr = addr
        self.handler = handler
        _FakeServer.instances.append(self)


@pytest.mark.unit
def test_bind_clean(monkeypatch):
    from quarry import gui

    _FakeServer.instances = []
    monkeypatch.setattr(gui, "ThreadingHTTPServer", _FakeServer)
    server, port = gui._bind("127.0.0.1", 8765)
    assert port == 8765
    assert isinstance(server, _FakeServer) and server.addr == ("127.0.0.1", 8765)


@pytest.mark.unit
def test_bind_reraises_non_addrinuse(monkeypatch):
    from quarry import gui

    def ctor(addr, handler):
        raise OSError(errno.EACCES, "permission denied")   # not 48/98

    monkeypatch.setattr(gui, "ThreadingHTTPServer", ctor)
    with pytest.raises(OSError) as ei:
        gui._bind("127.0.0.1", 80)
    assert ei.value.errno == errno.EACCES


@pytest.mark.unit
def test_bind_reclaim_and_takeover(monkeypatch, capsys):
    """errno 48 once, then _reclaim_port True -> rebind SAME port and take over."""
    from quarry import gui

    _FakeServer.instances = []
    state = {"raised": False}

    def ctor(addr, handler):
        if not state["raised"]:
            state["raised"] = True
            raise OSError(48, "address already in use")
        return _FakeServer(addr, handler)

    monkeypatch.setattr(gui, "ThreadingHTTPServer", ctor)
    monkeypatch.setattr(gui, "_reclaim_port", lambda port: True)
    monkeypatch.setattr(gui, "_next_free_port",
                        lambda *a, **k: pytest.fail("must not move ports when we reclaimed"))

    server, port = gui._bind("127.0.0.1", 8765)
    assert port == 8765                                   # SAME port
    assert server.addr == ("127.0.0.1", 8765)
    assert "took over" in capsys.readouterr().out


@pytest.mark.unit
def test_bind_foreign_moves_to_next_free(monkeypatch, capsys):
    """errno 98 once, _reclaim_port False (foreign) -> bind next free port."""
    from quarry import gui

    _FakeServer.instances = []
    state = {"raised": False}

    def ctor(addr, handler):
        if not state["raised"]:
            state["raised"] = True
            raise OSError(98, "address already in use")   # linux EADDRINUSE
        return _FakeServer(addr, handler)

    monkeypatch.setattr(gui, "ThreadingHTTPServer", ctor)
    monkeypatch.setattr(gui, "_reclaim_port", lambda port: False)
    monkeypatch.setattr(gui, "_next_free_port", lambda host, start, tries=30: 8899)

    server, port = gui._bind("127.0.0.1", 8765)
    assert port == 8899                                   # moved
    assert server.addr == ("127.0.0.1", 8899)
    assert "not Quarry" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# a couple of in-process integration checks against the real Postgres
# (prove the mocked branches match real behavior end-to-end, coverage counts)
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_api_health_real_postgres_ok(ws, isolated_cache):
    out = isolated_cache.api_health("testpg", "test", fresh=True)
    assert out == {"ok": True}
    # and it was cached with a timestamp
    stored = isolated_cache._cache_get("health:testpg@test")
    assert stored["ok"] is True and "_ts" in stored


@requires_db
@pytest.mark.integration
def test_list_tables_real_postgres(ws, isolated_cache):
    conn = isolated_cache._resolve("testpg", "test")
    tables = isolated_cache._list_tables(conn)
    assert "customers" in tables and "orders" in tables


@requires_db
@pytest.mark.integration
def test_api_columns_real_postgres(ws, isolated_cache):
    out = isolated_cache.api_columns("testpg", "test", "customers")
    assert "id" in out["columns"] and "email" in out["columns"]
    # second call served from cache -> identical
    assert isolated_cache.api_columns("testpg", "test", "customers") == out
