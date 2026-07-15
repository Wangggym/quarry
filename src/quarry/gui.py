"""Quarry GUI — a local, zero-dependency data viewer (stdlib http.server).

One more *face* over the core engine: browse connections grouped into project
folders and env-sets, pick a table (or Redis key), run read-only SQL, and read
a polished data grid. Slate & Copper theme, light/dark.

Launch:  qy gui            (or: python -m quarry.gui)
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from . import __version__, core, local, local_sync, redis_engine, tunnel, workspace
from .core import QuarryError

log = logging.getLogger("quarry.gui")

_WEB_DIST = Path(__file__).resolve().parent / "web_dist"


def _setup_logging() -> None:
    if log.handlers:
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

# Server-side cache so table lists + health survive browser reloads AND `qy gui`
# restarts. NO expiry — entries live until replaced by a fresh (fresh=1) call.
# Persisted to disk (JSON) so it outlives the process. key -> value.
_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_FILE = Path.home() / ".cache" / "quarry" / "gui-cache.json"


def _load_cache() -> None:
    try:
        with _CACHE_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _CACHE.update(data)
    except Exception:
        pass


def _save_cache() -> None:
    try:
        with _CACHE_LOCK:
            snapshot = dict(_CACHE)
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def _cache_get(key: str):
    return _CACHE.get(key)


def _cache_put(key: str, value: dict) -> dict:
    _CACHE[key] = value
    _save_cache()
    return value


# ---------------------------------------------------------------------------
# Events (SSE) — the GUI's change-notification contract
# ---------------------------------------------------------------------------
# One channel, `GET /api/events`, streams JSON events of the shape
# {"type": <str>, "ts": <epoch seconds>}. Events are *hints to refetch*, never
# data carriers — losing one is harmless, so there is no replay/Last-Event-ID.
#
# Event types (the contract consumed by web/src/useEvents.ts and by future
# features building on this channel):
#   workspace_changed — a watched workspace file (config.toml, any
#                       connections.toml, any queries/**/*.sql) changed on
#                       disk; clients should refetch connections + queries.
#   update_available  — the background update checker (below) found a newer
#                       quarry-db release on PyPI; clients should refetch
#                       GET /api/update to paint the header badge.
#
# A comment line (`: hb`) is sent every HEARTBEAT_SEC as keep-alive; clients
# also use the EventSource auto-reconnect that follows a server restart to
# re-check /api/version and prompt a page reload after an upgrade.

HEARTBEAT_SEC = float(os.environ.get("QUARRY_EVENTS_HEARTBEAT", "15"))
WATCH_INTERVAL_SEC = float(os.environ.get("QUARRY_WATCH_INTERVAL", "2"))

_SUBSCRIBERS: set[queue.Queue] = set()
_SUB_LOCK = threading.Lock()
_WATCHER_STARTED = False


def publish_event(type_: str) -> None:
    evt = {"type": type_, "ts": time.time()}
    with _SUB_LOCK:
        subs = list(_SUBSCRIBERS)
    for q in subs:
        try:
            q.put_nowait(evt)
        except queue.Full:  # slow consumer: drop — events are refetch hints
            pass


def _sse_format(evt: dict) -> bytes:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8")


def _close_event_streams() -> None:
    """Ask every open SSE handler to end its stream (each blocks on its own
    queue, so a normal event is the only way to wake them). Process exit does
    this implicitly for `qy gui`; serve() and tests use it for a clean stop."""
    publish_event("_close")


def _ws_fingerprint() -> dict[str, float]:
    """mtime of every watched file: the workspace-list config plus each
    workspace's connections.toml and queries/**/*.sql. Dict compare catches
    edits, additions, and deletions alike."""
    fp: dict[str, float] = {}
    paths = [workspace._config_path()]
    for w in workspace.WS_LIST:
        paths.append(w.connections_file)
        try:
            paths.extend(sorted(w.queries_dir.rglob("*.sql")))
        except OSError:
            pass
    for p in paths:
        try:
            fp[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return fp


def _apply_workspace_change() -> None:
    """React to an on-disk workspace change: re-resolve the workspace list
    (config.toml may have changed), drop health cache entries (connection URLs
    may now differ, making cached probe results lies), and notify clients.
    Table/column caches survive — they are keyed by db@env and refresh via the
    existing fresh=1 path."""
    workspace.reload_workspace()
    for k in [k for k in list(_CACHE) if k.startswith("health:")]:
        _CACHE.pop(k, None)
    _save_cache()
    publish_event("workspace_changed")


def _watch_tick(prev: dict[str, float]) -> dict[str, float]:
    """One watcher iteration: compare fingerprints, apply on change. Split out
    of the loop so tests can drive it deterministically."""
    cur = _ws_fingerprint()
    if cur != prev:
        try:
            _apply_workspace_change()
        except Exception:  # noqa: BLE001 — watcher must survive bad configs
            log.exception("workspace watcher: reload failed")
    return cur


def _watch_loop() -> None:  # pragma: no cover — thread wrapper around _watch_tick
    fp = _ws_fingerprint()
    while True:
        time.sleep(WATCH_INTERVAL_SEC)
        fp = _watch_tick(fp)


def _ensure_watcher() -> None:
    """Start the (single, daemon) file watcher lazily on first SSE subscriber —
    no client listening means nobody to notify, so no thread until then."""
    global _WATCHER_STARTED
    with _SUB_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True
    threading.Thread(target=_watch_loop, name="quarry-gui-watcher", daemon=True).start()


# ---------------------------------------------------------------------------
# Update check — PyPI polling for a newer quarry-db release
# ---------------------------------------------------------------------------
# A background daemon thread, throttled to once per UPDATE_CHECK_INTERVAL_SEC
# (default 24h, tracked via a `checked_at` timestamp persisted in gui-cache so
# the throttle survives `qy gui` restarts), asks PyPI's JSON API for the
# latest quarry-db release. A newer version publishes `update_available` over
# /api/events; GET /api/update reports the last-known state for the header's
# first paint (before any event fires).
#
# Silent by design: QUARRY_UPDATE_CHECK=0, an editable/dev install (nothing to
# `pipx upgrade`), or any network failure all mean "no update" with nothing
# surfaced anywhere — this channel must never become noise or a false alarm.

PYPI_URL = os.environ.get("QUARRY_PYPI_URL", "https://pypi.org/pypi/quarry-db/json")
UPDATE_CHECK_INTERVAL_SEC = float(os.environ.get("QUARRY_UPDATE_INTERVAL", str(24 * 3600)))
UPDATE_LOOP_SLEEP_SEC = float(os.environ.get("QUARRY_UPDATE_LOOP_SLEEP", "3600"))
_UPDATE_CACHE_KEY = "update_check"
_UPDATE_CHECKER_STARTED = False
_UPDATE_CHECKER_LOCK = threading.Lock()


def _update_check_disabled() -> bool:
    return os.environ.get("QUARRY_UPDATE_CHECK", "") == "0"


def _is_editable_install() -> bool:
    """True for `pip install -e` / source-checkout installs — an editable
    install has no PyPI wheel to upgrade into, so checking is pointless noise
    for contributors. Detected via the `direct_url.json` dist-info metadata
    pip writes for such installs (`dir_info.editable: true`); a normal PyPI
    install has no direct_url.json at all."""
    try:
        import importlib.metadata as importlib_metadata

        dist = importlib_metadata.distribution("quarry-db")
        raw = dist.read_text("direct_url.json")
        if not raw:
            return False
        info = json.loads(raw)
        return bool(info.get("dir_info", {}).get("editable"))
    except Exception:  # noqa: BLE001 — never let detection break the checker
        return False


def _parse_version(v: str) -> tuple[int, ...]:
    """Numeric dot-segments only (ignores any pre-release suffix) — enough to
    compare quarry-db's plain MAJOR.MINOR.PATCH releases by segment rather
    than as strings (so 0.10.0 > 0.9.0). Never raises on odd input."""
    out = []
    for part in (v or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _version_gt(a: str, b: str) -> bool:
    return _parse_version(a) > _parse_version(b)


def _fetch_latest_version(timeout: float = 5.0) -> str | None:
    """The latest version PyPI reports for quarry-db, or None on ANY failure
    (network, timeout, bad JSON) — callers must treat None as "couldn't check
    this time", never as an error to surface."""
    try:
        req = Request(PYPI_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        v = data.get("info", {}).get("version")
        return v if isinstance(v, str) and v else None
    except Exception:  # noqa: BLE001 — silent by design, see module docstring
        return None


def _check_for_update(force: bool = False) -> None:
    """One throttled check: skip entirely if disabled/editable; skip the PyPI
    call if the last check is still within the interval (unless forced);
    otherwise fetch + cache the result and publish an event if a newer
    release appeared. `checked_at` advances on every real attempt (even a
    failed one) so a PyPI outage can't turn into a retry storm."""
    if _update_check_disabled() or _is_editable_install():
        return
    c = _cache_get(_UPDATE_CACHE_KEY) or {}
    last = c.get("checked_at")
    if not force and isinstance(last, (int, float)) and (time.time() - last) < UPDATE_CHECK_INTERVAL_SEC:
        return
    latest = _fetch_latest_version()
    now = time.time()
    if latest is None:
        _cache_put(_UPDATE_CACHE_KEY, {**c, "checked_at": now})
        return
    available = _version_gt(latest, __version__)
    _cache_put(_UPDATE_CACHE_KEY, {"checked_at": now, "latest": latest, "available": available})
    if available:
        publish_event("update_available")


def _update_loop() -> None:  # pragma: no cover — thread wrapper around _check_for_update
    while True:
        try:
            _check_for_update()
        except Exception:  # noqa: BLE001 — checker must survive a bad response
            log.exception("update checker: check failed")
        time.sleep(UPDATE_LOOP_SLEEP_SEC)


def _ensure_update_checker() -> None:
    """Start the (single, daemon) update-check thread. Unlike the workspace
    watcher this isn't gated on an SSE subscriber — GET /api/update must have
    something to report on the very first page load."""
    global _UPDATE_CHECKER_STARTED
    with _UPDATE_CHECKER_LOCK:
        if _UPDATE_CHECKER_STARTED:
            return
        _UPDATE_CHECKER_STARTED = True
    threading.Thread(target=_update_loop, name="quarry-gui-update-checker", daemon=True).start()


def api_update() -> dict:
    """Last-known update-check state — never triggers a network call itself
    (that's the background thread's job); this just reads the cache."""
    c = _cache_get(_UPDATE_CACHE_KEY) or {}
    return {
        "current": __version__,
        "latest": c.get("latest"),
        "available": bool(c.get("available")),
    }


def _resolve(db: str, env: str | None):
    return core.resolve_connection(db, env)


def _list_tables(conn) -> list[str]:
    engine = core.connection_engine(conn)
    if engine == "redis":
        with tunnel.open_tunnel(conn, engine) as url:
            return redis_engine.scan_keys(url, count=1000)
    if engine == "mysql":
        sql = ("SELECT table_name FROM information_schema.tables "
               "WHERE table_schema = DATABASE() ORDER BY table_name")
    elif engine == "neptune":
        return []
    else:
        sql = ("SELECT table_name FROM information_schema.tables "
               "WHERE table_schema = 'public' ORDER BY table_name")
    res = core.run_query(conn, sql, max_rows=5000)
    return [r.get("table_name") for r in res.rows if r.get("table_name")]


def _display_path(p) -> str:
    """Home-relative display path — keeps usernames out of the UI (and screenshots)."""
    s, home = str(p), str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def api_connections() -> dict:
    homes = [_display_path(w.home) for w in workspace.WS_LIST]
    groups = core.group_connections()
    for g in groups:
        if g.get("ws"):
            g["ws"] = _display_path(g["ws"])
    return {"groups": groups, "workspace": _display_path(workspace.WS.home), "workspaces": homes}


def api_workspaces() -> dict:
    """The config.toml-registered workspace list, for the header's manage-UI
    (issue #15 — that list used to be display-only). Mirrors `qy workspace
    list`: raw dirs as written, each flagged if missing or lacking a
    connections.toml, so a typo is visible before it silently contributes
    nothing."""
    items = []
    for d in workspace.config_workspaces():
        home = Path(d).expanduser()
        items.append({
            "dir": d,
            "display": _display_path(home),
            "exists": home.exists(),
            "hasConnections": (home / "connections.toml").exists(),
        })
    return {"config": _display_path(workspace._config_path()), "items": items}


def api_workspace_add(body: dict) -> dict:
    workspace.add_workspace(_req(body, "dir"))
    # reload_workspace(), not configure_workspace(None): the GUI is a long-lived
    # process that may have been launched with an explicit --workspace override,
    # which a hardcoded None would silently drop.
    workspace.reload_workspace()
    return api_workspaces()


def api_workspace_remove(body: dict) -> dict:
    d = _req(body, "dir")
    if not workspace.remove_workspace(d):
        raise QuarryError(f"workspace not found in config: {d}")
    workspace.reload_workspace()
    return api_workspaces()


def api_tables(db: str, env: str | None, fresh: bool = False) -> dict:
    key = f"tables:{db}@{env}"
    if not fresh:
        c = _cache_get(key)
        if c is not None:
            return {**c, "_cached": True}
    conn = _resolve(db, env)
    engine = core.connection_engine(conn)
    if engine == "redis":
        with tunnel.open_tunnel(conn, engine) as url:
            ks = redis_engine.keys_with_meta(url, cap=400)
            # `capped` tells the UI the key list was cut off (never silently truncate)
            out = {"engine": "redis", "keys": ks, "capped": len(ks) >= 400}
    else:
        ts = _list_tables(conn)
        # `capped` tells the UI the list hit _list_tables' 5000-row cap
        out = {"tables": ts, "engine": engine, "capped": len(ts) >= 5000}
    _cache_put(key, out)
    return {**out, "_cached": False}


def api_columns(db: str, env: str | None, table: str) -> dict:
    """Column names + types for one table (postgres/mysql), cached. `columns`
    (a flat name list) powers editor autocomplete of `table.<col>`; `types`
    (name -> data_type) powers the sidebar's table-structure browser. Never
    raises — returns {columns: [], types: {}} on any miss.

    The table name is matched via a bound `:'table'` query parameter (psql -v
    for postgres, our own quote_val escaping for mysql) rather than a
    character-stripping sanitizer — a stripped name silently dropped legal
    quoted/special-character table names (e.g. `qy-review-weird`) that
    `/api/tables` had just listed, so clicking them showed an empty schema."""
    if not (table or "").strip():
        return {"columns": [], "types": {}}
    key = f"columns:{db}@{env}:{table}"
    c = _cache_get(key)
    if c is not None:
        return c
    try:
        conn = _resolve(db, env)
        engine = core.connection_engine(conn)
        if engine in ("redis", "neptune"):
            return _cache_put(key, {"columns": [], "types": {}})
        schema = "DATABASE()" if engine == "mysql" else "'public'"
        sql = ("SELECT column_name, data_type FROM information_schema.columns "
               f"WHERE table_schema = {schema} AND table_name = :'table' "
               "ORDER BY ordinal_position")
        res = core.run_query(conn, sql, params={"table": table}, max_rows=2000)
        cols = [r.get("column_name") for r in res.rows if r.get("column_name")]
        types = {r.get("column_name"): r.get("data_type")
                 for r in res.rows if r.get("column_name")}
        return _cache_put(key, {"columns": cols, "types": types})
    except Exception:  # noqa: BLE001
        return {"columns": [], "types": {}}


def api_inspect(db: str, env: str | None, key: str) -> dict:
    if not key:
        raise QuarryError("inspect requires a 'key' parameter")
    conn = _resolve(db, env)
    engine = core.connection_engine(conn)
    if engine != "redis":
        raise QuarryError(f"inspect is redis-only (connection '{db}' is {engine})")
    with tunnel.open_tunnel(conn, engine) as url:
        rows = redis_engine.inspect_key(url, key)
    return {"columns": core._columns_from_rows(rows), "rows": rows, "rowCount": len(rows),
            "truncated": False, "elapsedMs": 0, "engine": "redis", "sql": f"# inspect {key}"}


_URL_PASSWORD_RE = re.compile(r"(://[^/@?#]*?):[^/@?#]*@")


def _mask_url(url: str) -> str:
    """Mask the password in a connection URL — the info panel must never leak
    credentials into screenshots or screen shares."""
    return _URL_PASSWORD_RE.sub(r"\1:••••@", url)


def api_conninfo(db: str, env: str | None, reveal: bool = False) -> dict:
    """Resolved-connection details for the info panel: what quarry will actually
    dial for this db@env, and which file that came from. Diagnosing "why can't I
    connect" starts with seeing the config that is really in effect.

    reveal=True returns the URL with its password — the GUI is localhost-only
    and the value already lives in the user's own connections.toml; the mask is
    a screenshot/screen-share guard, not an access control."""
    conn = _resolve(db, env)
    p = urlparse(conn.url)
    file = workspace.WS.connections_file
    for w in workspace.WS_LIST:
        if str(w.home) == (conn.source or ""):
            file = w.connections_file
            break
    out = {
        "key": conn.key, "db": conn.logical_db, "env": conn.env,
        "engine": core.connection_engine(conn),
        "url": conn.url if reveal else _mask_url(conn.url),
        "host": p.hostname, "port": p.port,
        "database": (p.path or "").lstrip("/") or None,
        "group": conn.group, "region": conn.region, "notes": conn.notes,
        "file": _display_path(file), "tunnel": None,
    }
    if conn.ssh_host:
        out["tunnel"] = {"host": conn.ssh_host, "user": conn.ssh_user,
                         "port": conn.ssh_port or 22, "key": conn.ssh_key}
    return out


def api_local_up(body: dict) -> dict:
    """GUI counterpart of `qy local up <db>`: start the shared local container
    for the env-set's engine and register an env=local connection."""
    db = _req(body, "db")
    conns = core.load_connections()
    members = {(c.env or ""): c for c in conns.values() if c.logical_db == db}
    if not members:
        raise QuarryError(f"unknown connection '{db}'")
    src = (members.get(core.DEFAULT_ENV)
           or next((m for e, m in sorted(members.items()) if e.lower() != local.LOCAL_ENV),
                   members[sorted(members)[0]]))
    engine = core.connection_engine(src)
    if engine not in local.SPECS:
        raise QuarryError(
            f"engine '{engine}' has no local-container support (postgres/redis only)")
    if not local.SAFE_DB_RE.match(db):
        raise QuarryError(f"'{db}' is not a valid local db name")
    spec = local.SPECS[engine]
    state = local.start_container(spec, image=local.stored_local_image(db))
    redis_db = local.source_redis_db(db) if engine == "redis" else None
    key, created = local.register_local_connection(
        db, spec, group=src.group, redis_db=redis_db)
    out = {"key": key, "created": created, "engine": engine,
           "state": state, "port": spec.port}
    if engine == "postgres":
        if not local.wait_pg_ready(spec):
            raise QuarryError("local postgres did not become ready in time")
        local.ensure_pg_database(spec, db)
        # First-time convenience: a freshly registered local env is an empty
        # shell — fill it from the remote sibling right away. A sync failure
        # must not undo the successful `up`; it is reported alongside.
        src_env = (src.env or "").lower()
        if created and src_env and src_env != local.LOCAL_ENV:
            try:
                res = api_local_sync({"db": db, "from": src.env})
                out["synced_from"] = res["from"]
            except Exception as e:  # noqa: BLE001
                out["sync_error"] = str(e)
    return out


def api_local_sync(body: dict) -> dict:
    """GUI counterpart of `qy local sync <db> [--from env]`. All safety gates
    (env=local + loopback host + no tunnel, postgres-only) live in sync_schema."""
    db = _req(body, "db")
    from_env = body.get("from") or core.DEFAULT_ENV
    res = local_sync.sync_schema(db, from_env=from_env)
    # the swapped-in database invalidates cached table lists for this env-set
    for k in [k for k in list(_CACHE) if k.startswith(f"tables:{db}@")]:
        _CACHE.pop(k, None)
    _save_cache()
    return {**res, "from": from_env}


HEALTH_TTL_SEC = int(os.environ.get("QUARRY_HEALTH_TTL", "120"))


def _health_fresh_enough(c: dict) -> bool:
    """A cached health entry is usable if it is younger than HEALTH_TTL_SEC.
    Legacy entries without a timestamp are treated as expired (re-probed)."""
    ts = c.get("_ts")
    return isinstance(ts, (int, float)) and (time.time() - ts) < HEALTH_TTL_SEC


def api_health(db: str, env: str | None, fresh: bool = False, cached_only: bool = False) -> dict:
    """Fast connectivity probe. Never raises — returns {ok, error}. Cached for
    HEALTH_TTL_SEC (default 120s) so reloads paint dots instantly but a transient
    failure self-heals. cached_only=True returns a still-fresh cache entry or
    {ok:None} without probing (used to paint dots instantly on page load)."""
    key = f"health:{db}@{env}"
    c = _cache_get(key)
    if not fresh and c is not None and _health_fresh_enough(c):
        return {k: v for k, v in c.items() if k != "_ts"}
    if cached_only:
        return {"ok": None}
    try:
        conn = _resolve(db, env)
        engine = core.connection_engine(conn)
        with tunnel.open_tunnel(conn, engine) as url:
            if engine == "redis":
                redis_engine.run_redis(url, "PING", timeout=6)
            elif engine == "mysql":
                core.run_mysql_query(url, "SELECT 1", timeout=6)
            elif engine == "neptune":
                core.run_neptune_cypher(url, "RETURN 1 AS ok", timeout=6)
            else:
                rc, _out, e = core.run_psql_capture(url, "SELECT 1", timeout=6)
                if rc != 0:
                    return _put_health(key, {"ok": False, "error": (e.strip() or "connect failed")[:200]})
        return _put_health(key, {"ok": True})
    except Exception as e:  # noqa: BLE001
        return _put_health(key, {"ok": False, "error": str(e)[:200]})


def _put_health(key: str, value: dict) -> dict:
    """Persist a health result with a timestamp; return it without the timestamp."""
    _cache_put(key, {**value, "_ts": time.time()})
    return value


def api_queries() -> list[dict]:
    return [{"name": q.name, "db": q.db, "desc": q.desc, "sql": q.sql,
             "params": [{"name": p.name, "type": p.type, "required": p.required, "default": p.default}
                        for p in q.params]}
            for q in core.list_all_queries()]


def _req(body: dict, field: str):
    val = body.get(field)
    if val is None or val == "":
        raise QuarryError(f"missing required field '{field}'")
    return val


def _max_rows(body: dict) -> int:
    try:
        return int(body.get("maxRows") or 500)
    except (TypeError, ValueError):
        raise QuarryError(f"maxRows must be an integer, got {body.get('maxRows')!r}")


def _offset(body: dict) -> int:
    try:
        return int(body.get("offset") or 0)
    except (TypeError, ValueError):
        raise QuarryError(f"offset must be an integer, got {body.get('offset')!r}")


def api_query(body: dict) -> dict:
    conn = _resolve(_req(body, "db"), body.get("env"))
    res = core.run_query(
        conn, _req(body, "sql"), max_rows=_max_rows(body), offset=_offset(body), with_types=True
    )
    return res.to_dict()


def api_run(body: dict) -> dict:
    q = core.load_query(_req(body, "name"))
    conn = _resolve(q.db, body.get("env"))
    params = core.resolve_params(q, body.get("params") or {})
    res = core.run_query(
        conn, q.sql, params=params, max_rows=_max_rows(body), offset=_offset(body), with_types=True
    )
    out = res.to_dict()
    # A saved query runs on its OWN connection (q.db), which may differ from the
    # tab that launched it. Report the producing connection so the client tags &
    # persists the result under it instead of the tab's current connection.
    out["db"] = conn.logical_db
    out["env"] = conn.env
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _local_origin_ok(self) -> bool:
        """Reject DNS-rebinding (foreign Host) and cross-site XHR (foreign Origin).
        The GUI is localhost-only and unauthenticated — without this, any web page
        the user visits could POST queries to the local API."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in _LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            oh = urlparse(origin).hostname or ""
            if oh not in _LOCAL_HOSTS:
                return False
        return True

    def _send(self, code, payload, content_type="application/json"):
        data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if content_type == "application/json"
                else (payload.encode("utf-8") if isinstance(payload, str) else payload))
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, code: int, path: Path) -> None:
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        content_type = mime or "application/octet-stream"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self) -> None:
        """SSE stream (see the Events contract above). Runs in this handler's
        own thread (ThreadingHTTPServer, daemon threads) until the client
        disconnects — detected by the failed heartbeat/event write."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=64)
        with _SUB_LOCK:
            _SUBSCRIBERS.add(q)
        _ensure_watcher()
        log.info("GET /api/events (stream opened)")
        try:
            self.wfile.write(_sse_format({"type": "hello", "ts": time.time()}))
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=HEARTBEAT_SEC)
                    if evt.get("type") == "_close":  # graceful server stop
                        break
                    self.wfile.write(_sse_format(evt))
                except queue.Empty:
                    self.wfile.write(b": hb\n\n")
                self.wfile.flush()
        except OSError:  # BrokenPipe/ConnectionReset — client went away
            pass
        finally:
            with _SUB_LOCK:
                _SUBSCRIBERS.discard(q)

    def _serve_react_app(self, path: str) -> bool:
        """Serve the Vite-built React app — the GUI's only frontend, under /app/."""
        if path == "/app":
            self.send_response(301)
            self.send_header("Location", "/app/")
            self.end_headers()
            return True
        if not path.startswith("/app/"):
            return False
        root = _WEB_DIST.resolve()
        rel = path[len("/app/"):] or "index.html"
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)):
            self._send(403, {"error": "forbidden"})
            return True
        if not target.is_file():
            target = root / "index.html"
            if not target.is_file():
                self._send(404, {"error": "react app not built"})
                return True
        self._send_file(200, target)
        return True

    def _err(self, exc, path):
        if isinstance(exc, QuarryError):
            log.warning("%s → %s (code=%s)", path, exc, exc.exit_code)
        else:
            log.error("%s → %s\n%s", path, exc, traceback.format_exc())
        self._send(400, {"error": str(exc), "code": getattr(exc, "exit_code", None)})

    def do_GET(self):
        if not self._local_origin_ok():
            return self._send(403, {"error": "forbidden: non-local origin"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        g = lambda k: (qs.get(k) or [None])[0]
        flag = lambda k: str(g(k) or "").lower() not in ("", "0", "false", "no")
        t0 = time.monotonic()
        try:
            if u.path in ("/", "/index.html"):
                self.send_response(301)
                self.send_header("Location", "/app/")
                self.end_headers()
                return
            if self._serve_react_app(u.path):
                return
            if u.path == "/api/events":
                return self._serve_events()
            if u.path == "/api/version":
                out = {"name": "Quarry", "version": __version__}
            elif u.path == "/api/connections":
                out = api_connections()
            elif u.path == "/api/workspaces":
                out = api_workspaces()
            elif u.path == "/api/tables":
                out = api_tables(g("db"), g("env"), fresh=flag("fresh"))
            elif u.path == "/api/inspect":
                out = api_inspect(g("db"), g("env"), g("key"))
            elif u.path == "/api/columns":
                out = api_columns(g("db"), g("env"), g("table"))
            elif u.path == "/api/queries":
                out = api_queries()
            elif u.path == "/api/health":
                out = api_health(g("db"), g("env"), fresh=flag("fresh"), cached_only=flag("cached"))
            elif u.path == "/api/conninfo":
                out = api_conninfo(g("db"), g("env"), reveal=flag("reveal"))
            elif u.path == "/api/update":
                out = api_update()
            else:
                return self._send(404, {"error": "not found"})
            log.info("GET %s (%d ms)", self.path, int((time.monotonic() - t0) * 1000))
            self._send(200, out)
        except BaseException as e:  # noqa: BLE001  (catch SystemExit too)
            self._err(e, "GET " + self.path)

    def do_POST(self):
        if not self._local_origin_ok():
            return self._send(403, {"error": "forbidden: non-local origin"})
        u = urlparse(self.path)
        t0 = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if u.path == "/api/query":
                out = api_query(body)
            elif u.path == "/api/run":
                out = api_run(body)
            elif u.path == "/api/local/up":
                out = api_local_up(body)
            elif u.path == "/api/local/sync":
                out = api_local_sync(body)
            elif u.path == "/api/workspaces/add":
                out = api_workspace_add(body)
            elif u.path == "/api/workspaces/remove":
                out = api_workspace_remove(body)
            else:
                return self._send(404, {"error": "not found"})
            log.info("POST %s (%d ms)", u.path, int((time.monotonic() - t0) * 1000))
            self._send(200, out)
        except BaseException as e:  # noqa: BLE001
            self._err(e, "POST " + u.path)


def _port_pids(port: int) -> list[int]:
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                           capture_output=True, text=True, timeout=3)
        return [int(x) for x in r.stdout.split()]
    except Exception:
        return []


def _is_quarry_gui(pid: int) -> bool:
    """True only for *our own* quarry GUI process — never a foreign 'gui' command,
    so _reclaim_port can't SIGTERM someone else's server on the same port."""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=3)
        c = r.stdout.lower()
        if "gui" not in c:
            return False
        # require an unambiguous quarry/qy marker in the command line
        return "quarry" in c or "/qy " in c or c.strip().endswith("/qy") or "-m quarry" in c
    except Exception:
        return False


def _reclaim_port(port: int) -> bool:
    """Kill a previous quarry-gui listening on `port` (never a foreign process)."""
    killed = False
    for pid in _port_pids(port):
        if pid != os.getpid() and _is_quarry_gui(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except Exception:
                pass
    if killed:
        time.sleep(0.8)
    return killed


def _next_free_port(host: str, start: int, tries: int = 30) -> int:
    for p in range(start + 1, start + 1 + tries):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def _bind(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    try:
        return ThreadingHTTPServer((host, port), Handler), port
    except OSError as e:
        if e.errno not in (48, 98):   # not address-in-use
            raise
        if _reclaim_port(port):       # our own old instance -> take it over
            print(f"port {port}: stopped the previous Quarry GUI and took over.", flush=True)
            return ThreadingHTTPServer((host, port), Handler), port
        new_port = _next_free_port(host, port)  # someone else -> move over
        print(f"port {port} is taken (not Quarry) — using {new_port} instead.", flush=True)
        return ThreadingHTTPServer((host, new_port), Handler), new_port


def serve(host="127.0.0.1", port=8765, ws_path=None, open_browser=True) -> int:  # pragma: no cover
    # blocking serve_forever loop — exercised by the real `qy gui` e2e, not the in-process gate
    workspace.configure_workspace(ws_path)
    _setup_logging()
    _load_cache()
    _ensure_update_checker()
    httpd, port = _bind(host, port)
    url = f"http://{host}:{port}"
    homes = ", ".join(str(w.home) for w in workspace.WS_LIST)
    print(f"Quarry GUI → {url}", flush=True)
    print(f"  workspace(s): {homes}", flush=True)
    print("  requests + errors log below; Ctrl-C to stop.", flush=True)
    if open_browser:
        try:
            webbrowser.open(f"{url}/app/")
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        _close_event_streams()
        httpd.server_close()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    sys.exit(main())
