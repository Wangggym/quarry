"""Quarry GUI — a local, zero-dependency data viewer (stdlib http.server).

One more *face* over the core engine: browse connections grouped into project
folders and env-sets, pick a table (or Redis key), run read-only SQL, and read
a polished data grid. Slate & Copper theme, light/dark.

Launch:  qy gui            (or: python -m quarry.gui)
"""

from __future__ import annotations

import json
import logging
import os
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

from . import core, redis_engine, tunnel, workspace
from .core import QuarryError

log = logging.getLogger("quarry.gui")


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
    """Column names for one table (postgres/mysql), cached. Powers editor
    autocomplete of `table.<col>`. Never raises — returns {columns: []} on any miss."""
    safe = "".join(ch for ch in (table or "") if ch.isalnum() or ch in "_$")
    if not safe:
        return {"columns": []}
    key = f"columns:{db}@{env}:{safe}"
    c = _cache_get(key)
    if c is not None:
        return c
    try:
        conn = _resolve(db, env)
        engine = core.connection_engine(conn)
        if engine in ("redis", "neptune"):
            return _cache_put(key, {"columns": []})
        schema = "DATABASE()" if engine == "mysql" else "'public'"
        sql = ("SELECT column_name FROM information_schema.columns "
               f"WHERE table_schema = {schema} AND table_name = '{safe}' "
               "ORDER BY ordinal_position")
        res = core.run_query(conn, sql, max_rows=2000)
        cols = [r.get("column_name") for r in res.rows if r.get("column_name")]
        return _cache_put(key, {"columns": cols})
    except Exception:  # noqa: BLE001
        return {"columns": []}


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


def api_query(body: dict) -> dict:
    conn = _resolve(_req(body, "db"), body.get("env"))
    res = core.run_query(conn, _req(body, "sql"), max_rows=_max_rows(body), with_types=True)
    return res.to_dict()


def api_run(body: dict) -> dict:
    q = core.load_query(_req(body, "name"))
    conn = _resolve(q.db, body.get("env"))
    params = core.resolve_params(q, body.get("params") or {})
    res = core.run_query(conn, q.sql, params=params, max_rows=_max_rows(body), with_types=True)
    return res.to_dict()


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
                return self._send(200, INDEX_HTML, "text/html")
            if u.path == "/api/connections":
                out = api_connections()
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
    httpd, port = _bind(host, port)
    url = f"http://{host}:{port}"
    homes = ", ".join(str(w.home) for w in workspace.WS_LIST)
    print(f"Quarry GUI → {url}", flush=True)
    print(f"  workspace(s): {homes}", flush=True)
    print("  requests + errors log below; Ctrl-C to stop.", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    return serve()


INDEX_HTML = r"""<!doctype html>
<html lang="zh" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quarry</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A//www.w3.org/2000/svg%27%20viewBox%3D%270%200%2032%2032%27%3E%3Crect%20width%3D%2732%27%20height%3D%2732%27%20rx%3D%277%27%20fill%3D%27%23c0824f%27/%3E%3Ctext%20x%3D%2716%27%20y%3D%2717%27%20font-family%3D%27-apple-system%2CSegoe%20UI%2CRoboto%2Csans-serif%27%20font-size%3D%2720%27%20font-weight%3D%27700%27%20fill%3D%27%23241407%27%20text-anchor%3D%27middle%27%20dominant-baseline%3D%27central%27%3EQ%3C/text%3E%3C/svg%3E">
<style>
:root{
  --bg0:#0e1116; --bg1:#1c2028; --bg2:#242a33; --bg3:#2f3641;
  --line:#404a58; --line2:#525d6e;
  --fg:#eef0f3; --fg2:#c4cbd6; --fg3:#a3abb8;
  --accent:#c0824f; --accent-hi:#d4966a; --accent-ink:#241407;
  --red:#c0504a; --red-bg:#3a2222; --red-fg:#e79a93;
  --ok:#8fb48c; --ok-bg:#232f24;
  --num:#8fbcea; --uuid:#c79fe2; --ts:#6fc8b6; --bool:#9cc199; --null:#8e97a4;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,"SF Pro Text","Segoe UI",Roboto,sans-serif;
}
html[data-theme=light]{
  --bg0:#f4f3ee; --bg1:#ffffff; --bg2:#f0efe9; --bg3:#e7e5dd;
  --line:#d6d3c7; --line2:#bfbaa9;
  --fg:#1c1c19; --fg2:#54534b; --fg3:#7a7970;
  --accent:#b06a34; --accent-hi:#c17b43; --accent-ink:#ffffff;
  --red:#b23b34; --red-bg:#f6e3e1; --red-fg:#8f2f29;
  --ok:#4f7a48; --ok-bg:#e6efe2;
  --num:#215da0; --uuid:#6a3fa0; --ts:#137a68; --bool:#3f6a39; --null:#8a887e;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 var(--sans);background:var(--bg0);color:var(--fg);height:100vh;display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
header{display:flex;align-items:center;gap:11px;padding:10px 14px;background:var(--bg1);border-bottom:1px solid var(--line)}
.logo{width:20px;height:20px;border-radius:5px;background:var(--accent);color:var(--accent-ink);display:flex;align-items:center;justify-content:center;font-weight:600;font-size:12px}
.brand{font-weight:600;letter-spacing:.3px}
.ws{color:var(--fg3);font-size:12.5px}
.sp{flex:1}
.badge{font-size:12px;padding:3px 10px;border-radius:6px;border:1px solid var(--line2)}
.badge.prod{background:var(--red-bg);color:var(--red-fg);border-color:var(--red)}
.badge.ro{background:var(--ok-bg);color:var(--ok);border-color:transparent}
.iconbtn{background:none;border:0;color:var(--fg3);cursor:pointer;font-size:16px;padding:3px;border-radius:5px}
.iconbtn:hover{color:var(--fg);background:var(--bg2)}
main{flex:1;display:flex;min-height:0}
aside{width:244px;background:var(--bg1);border-right:1px solid var(--line);overflow:auto;padding:7px 0;flex:none}
.grp{display:flex;align-items:center;gap:7px;padding:9px 12px 4px;color:var(--fg2);font-size:12px;text-transform:uppercase;letter-spacing:.5px;cursor:pointer;user-select:none}
.grp .ti{font-size:13px}
.grp .wsorig{margin-left:auto;text-transform:none;letter-spacing:0;color:var(--fg3);font-size:10px;overflow:hidden;text-overflow:ellipsis;max-width:110px;white-space:nowrap}
.dbrow{display:flex;align-items:center;gap:8px;padding:6px 12px 6px 22px;color:var(--fg);cursor:pointer;border-radius:0}
.dbrow:hover{background:var(--bg2)}
.dbrow.on{background:var(--bg2);box-shadow:inset 2px 0 0 var(--accent);color:#fff}
html[data-theme=light] .dbrow.on{color:#000}
.dbrow .dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--fg3);opacity:.4;transition:background .2s}
.dbrow .dot.ok{background:#4e9a6b;opacity:.9}
.dbrow .dot.down{background:#cf5b52;opacity:.9}
.dbrow .dot.chk{opacity:.85;animation:pulse 1s ease-in-out infinite}
.dbrow.down{opacity:.5}
.dbrow.down:hover{opacity:.85}
@keyframes pulse{50%{opacity:.25}}
.dbrow small{margin-left:auto;color:var(--fg3);font-size:11.5px}
.pills{display:flex;flex-wrap:wrap;gap:4px;padding:1px 10px 5px 30px}
.pill{font-size:11.5px;padding:2px 9px;border-radius:10px;border:1px solid var(--line2);color:var(--fg2);cursor:pointer}
.pill.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.pill.on.prod{background:var(--red);border-color:var(--red);color:#fff}
.tname,.qname{display:flex;align-items:center;gap:8px;padding:5px 12px 5px 22px;color:var(--fg2);font-family:var(--mono);font-size:13px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tname:hover,.qname:hover{background:var(--bg2);color:var(--fg)}
.tname.on{background:var(--bg2);color:var(--fg);box-shadow:inset 2px 0 0 var(--accent)}
.tname .ti,.qname .ti{font-size:14px;color:var(--fg3);flex:none}
.trow{display:flex;align-items:center;gap:5px;margin:3px 12px 5px}
.trow .tsearch{flex:1;width:auto;margin:0}
.treload{background:none;border:0;color:var(--fg3);cursor:pointer;font-size:14px;padding:3px 4px;border-radius:5px;flex:none}
.treload:hover{color:var(--fg);background:var(--bg2)}
section{flex:1;display:flex;flex-direction:column;min-width:0}
.qhead{display:flex;align-items:center;gap:10px;padding:9px 14px;background:var(--bg1);border-bottom:1px solid var(--line);flex:none}
.qtitle{font-family:var(--mono);font-size:13.5px}
.runon{color:var(--fg3);font-size:12px}
.esw{display:flex;gap:5px}
.ep{font-size:12px;padding:3px 11px;border-radius:6px;border:1px solid var(--line2);color:var(--fg2);cursor:pointer}
.ep.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.ep.on.prod{background:var(--red);border-color:var(--red);color:#fff}
.edwrap{position:relative;background:var(--bg2);overflow:hidden;height:154px}
.hresizer{height:6px;flex:none;cursor:row-resize;background:var(--bg1);border-bottom:1px solid var(--line);position:relative}
.hresizer::after{content:"";position:absolute;left:50%;top:2px;width:30px;height:2px;transform:translateX(-50%);background:var(--fg3);opacity:.4;border-radius:2px}
.hresizer:hover{background:var(--bg3)}
.hresizer:hover::after{background:var(--accent);opacity:1}
.edwrap pre,.edwrap textarea{margin:0;padding:11px 14px;font-family:var(--mono);font-size:13.5px;line-height:1.6;border:0;white-space:pre-wrap;word-break:break-word;width:100%;height:100%;box-sizing:border-box}
.edwrap pre{position:absolute;inset:0;pointer-events:none;color:var(--fg);overflow:hidden}
.edwrap textarea{position:relative;background:transparent;color:transparent;caret-color:var(--accent);resize:none;outline:none;overflow:auto}
.tok-kw{color:var(--accent)}.tok-fn{color:var(--num)}.tok-str{color:var(--ok)}.tok-num{color:var(--num)}.tok-cm{color:var(--fg3)}
.toolbar{display:flex;align-items:center;gap:9px;padding:9px 14px;background:var(--bg1);border-bottom:1px solid var(--line);flex:none}
.btn{font-size:13px;color:var(--fg);background:var(--bg2);border:1px solid var(--line2);border-radius:6px;padding:6px 12px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--bg3)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:500}
.btn.primary:hover{background:var(--accent-hi)}
.gridwrap{flex:1;overflow:auto;position:relative}
table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-family:var(--mono);font-size:13px;table-layout:auto}
th{position:sticky;top:0;z-index:1;background:var(--bg2);color:var(--fg2);font-weight:500;text-align:left;padding:8px 24px 8px 12px;border-bottom:1px solid var(--line2);border-right:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
th .ty{color:var(--fg3);font-size:11px;margin-left:5px;font-weight:400}
th .ar{color:var(--accent);margin-left:4px;font-size:10px}
th .rz{position:absolute;right:0;top:0;width:6px;height:100%;cursor:col-resize}
th .rz:hover{background:var(--accent)}
td{padding:7px 12px;border-bottom:1px solid var(--line);border-right:1px solid var(--line);color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:480px;cursor:default}
tr:nth-child(even) td{background:var(--bg1)}
tr:hover td{background:var(--bg2)}
td.num{color:var(--num);text-align:right}td.uuid{color:var(--uuid)}td.ts{color:var(--ts)}td.bool{color:var(--bool)}td.json{color:var(--uuid)}
td.null{color:var(--null);font-style:italic}
td.sel{outline:2px solid var(--accent);outline-offset:-2px}
td.rownum{color:var(--fg3);text-align:right;cursor:pointer;background:var(--bg2);position:sticky;left:0}
td.rownum:hover{color:var(--accent)}
th.rownum{left:0;z-index:3;cursor:default;padding:6px 8px}
@keyframes spin{to{transform:rotate(360deg)}}
.spin .ti-loader{display:inline-block;animation:spin 1s linear infinite}
.resizer{width:5px;flex:none;cursor:col-resize;background:transparent}
.resizer:hover{background:var(--accent)}
.tsearch{width:calc(100% - 24px);margin:3px 12px 5px;padding:6px 10px;font-size:13px;background:var(--bg2);border:1px solid var(--line2);border-radius:6px;color:var(--fg);font-family:var(--mono)}
.tsearch::placeholder{color:var(--fg3)}
.rediskey{display:flex;align-items:center;gap:6px}
.rbadge{font-size:9.5px;padding:0 5px;border-radius:8px;background:var(--bg3);color:var(--fg2);margin-left:auto;flex:none}
.rbadge.ttl{color:var(--accent)}
.knode{cursor:pointer;user-select:none}
.kchild{padding-left:12px}
.status{display:flex;align-items:center;gap:14px;padding:7px 14px;background:var(--bg1);border-top:1px solid var(--line);color:var(--fg2);font-size:12.5px;flex:none}
.status .cu{color:var(--accent)}
.status .tr{color:var(--accent)}
.empty{color:var(--fg3);padding:22px;text-align:center}
.err{color:var(--red-fg);padding:14px;white-space:pre-wrap;font-family:var(--mono);font-size:12px}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--red-bg);color:var(--red-fg);border:1px solid var(--red);padding:8px 14px;border-radius:8px;font-size:12.5px;max-width:70%;z-index:50}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:60}
.modal .box{background:var(--bg1);border:1px solid var(--line2);border-radius:12px;max-width:70%;max-height:70%;overflow:auto;padding:16px}
.modal pre{margin:0;font-family:var(--mono);font-size:12.5px;white-space:pre-wrap;word-break:break-word;color:var(--fg)}
.modal .mh{display:flex;align-items:center;gap:8px;margin-bottom:10px;color:var(--fg2);font-size:12px}
.spin{color:var(--fg3);padding:22px;text-align:center}
.tabs{display:flex;gap:4px;padding:6px 10px 0;background:var(--bg1);flex:none;overflow-x:auto}
.tab{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-family:var(--mono);padding:4px 10px;border:1px solid var(--line);border-bottom:0;border-radius:7px 7px 0 0;color:var(--fg3);cursor:pointer;white-space:nowrap;background:var(--bg1);max-width:180px;overflow:hidden;text-overflow:ellipsis}
.tab.on{background:var(--bg2);color:var(--fg);border-color:var(--line2)}
.tab .x{color:var(--fg3);padding:0 2px;border-radius:3px;flex:none}
.tab .x:hover{color:var(--red-fg);background:var(--bg3)}
.tab.add{border-style:dashed;padding:4px 9px;flex:none}
.tab.add:hover{color:var(--accent)}
.jt{margin-left:14px}.jt>summary{cursor:pointer;color:var(--fg2);font-family:var(--mono);font-size:12.5px;user-select:none}
.jrow{margin-left:30px;font-family:var(--mono);font-size:12.5px;word-break:break-word}
.jk{color:var(--accent-hi)}.jstr{color:var(--ok)}.jnum{color:var(--num)}.jbool{color:var(--bool)}.jnull{color:var(--null);font-style:italic}
.jm{color:var(--fg3);font-size:11px}
.hsearch{width:100%;margin:0 0 8px;padding:6px 10px;font-size:13px;background:var(--bg2);border:1px solid var(--line2);border-radius:6px;color:var(--fg);font-family:var(--mono);box-sizing:border-box}
.hmeta{color:var(--fg3);font-size:11px;margin-top:3px;font-family:var(--sans)}
.acbox{position:fixed;z-index:80;background:var(--bg1);border:1px solid var(--line2);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.35);max-height:260px;overflow:auto;min-width:196px;padding:4px;font-family:var(--mono);font-size:13.5px;display:none}
.acitem{display:flex;align-items:center;gap:8px;padding:4px 8px;border-radius:5px;color:var(--fg);cursor:pointer;white-space:nowrap}
.acitem.on{background:var(--accent);color:var(--accent-ink)}
.acitem .ack{font-size:9px;opacity:.85;width:24px;flex:none;text-transform:uppercase}
.ack-tbl{color:var(--ts)}.ack-col{color:var(--num)}.ack-kw{color:var(--accent-hi)}
.acitem.on .ack,.acitem.on .ack-tbl,.acitem.on .ack-col,.acitem.on .ack-kw{color:var(--accent-ink)}
</style>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.0.0/dist/tabler-icons.min.css" media="print" onload="this.media='all'">
</head>
<body>
<header>
  <div class="logo">Q</div><span class="brand">Quarry</span>
  <span class="ws" id="ws"></span>
  <span class="sp"></span>
  <span class="badge prod" id="prodBadge" style="display:none"><i class="ti ti-alert-triangle"></i> prod</span>
  <span class="badge ro" id="roBadge"><i class="ti ti-lock"></i> read-only · auto LIMIT</span>
  <button class="iconbtn" id="healthBtn" title="Check all connections"><i class="ti ti-activity"></i></button>
  <button class="iconbtn" id="langBtn" style="font-size:13px;font-weight:600" title="Language">中</button>
  <button class="iconbtn" id="themeBtn" title="Toggle theme"><i class="ti ti-sun"></i></button>
</header>
<main>
  <aside id="side"><div class="spin"><i class="ti ti-loader"></i> Loading connections…</div></aside>
  <div class="resizer" id="resizer"></div>
  <section>
    <div class="qhead">
      <span class="qtitle" id="qtitle">No connection selected</span>
      <span class="runon" id="runon" style="display:none">runs on</span>
      <span class="esw" id="esw"></span>
      <span class="sp"></span>
    </div>
    <div class="tabs" id="tabs"></div>
    <div class="edwrap">
      <pre id="hl" aria-hidden="true"></pre>
      <textarea id="sql" spellcheck="false" placeholder="Pick a connection, then write SQL — Cmd/Ctrl+Enter runs"></textarea>
    </div>
    <div class="hresizer" id="edresizer" title="Drag to resize the editor"></div>
    <div class="toolbar">
      <button class="btn primary" id="runBtn"><i class="ti ti-player-play"></i> <span id="runLbl">Run</span></button>
      <button class="btn" id="fmtBtn"><i class="ti ti-wand"></i> <span id="fmtLbl">Format</span></button>
      <button class="btn" id="expBtn" title="Show query plan (EXPLAIN)"><i class="ti ti-route"></i> EXPLAIN</button>
      <button class="btn" id="csvBtn"><i class="ti ti-download"></i> CSV</button>
      <button class="btn" id="jsonBtn"><i class="ti ti-braces"></i> JSON</button>
      <select id="maxRows" class="btn" title="max rows" style="padding:5px 7px">
        <option>100</option><option selected>500</option><option>2000</option><option>5000</option>
      </select>
      <span class="sp"></span>
      <button class="btn" id="histBtn"><i class="ti ti-history"></i> <span id="histLbl">History</span></button>
    </div>
    <div class="gridwrap" id="grid"><div class="empty">Pick a connection and a table, or write some SQL.</div></div>
    <div class="status" id="status" style="display:none"></div>
  </section>
</main>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---- i18n: English default, 中文 via the 中/EN toggle ---- */
const I18N={
en:{loading:'Loading connections…',no_conn:'No connection selected',runs_on:'runs on',
 ph_sql:'Write SQL — Cmd/Ctrl+Enter runs',ph_sql_first:'Pick a connection, then write SQL — Cmd/Ctrl+Enter runs',
 ph_redis:'redis command, e.g. GET key / SCAN 0 / HGETALL key',
 run:'Run',fmt:'Format',hist:'History',ro_badge:'read-only · auto LIMIT',
 check_health:'Check all connections',toggle_theme:'Toggle theme',switch_lang:'切换到中文',
 drag_editor:'Drag to resize the editor',explain_title:'Show query plan (EXPLAIN)',
 empty_grid:'Pick a connection and a table, or write some SQL.',rows:'rows',truncated:'truncated to cap',
 row_detail:'Row detail',cell:'Cell · click outside to close ·',copy:'Copy',copied:'Copied',
 saved_queries:'saved queries',other:'other',params_suffix:'params',fill_params:'parameters',
 required:'*required',default_v:'default',
 filter_tables:'Filter tables…',filter_keys:'Filter keys…',no_tables:'no tables',no_keys:'no keys',
 running:'Running…',hist_title:'Query history',hist_search:'Search history… (SQL / connection)',
 no_match:'no match',no_hist:'No history yet',new_query:'new query',close_tab:'Close tab',new_tab:'New tab',
 just_now:'just now',min_ago:'m ago',hr_ago:'h ago',day_ago:'d ago',
 no_plan_redis:'no query plan for redis',pick_conn:'pick a connection first',empty_plan:'(empty plan)',
 prod_no_autorun:'switched to prod — press Run to execute',
 copy_fail:'copy failed — clipboard unavailable',max_rows:'max rows per query',
 keys_capped:'showing only the first {n} keys — narrow with the filter',
 list_capped:'showing only the first {n} tables — narrow with the filter',
 refresh_list:'refresh list',alt_insert:'Alt+click inserts the SQL without running'},
zh:{loading:'加载连接…',no_conn:'未选连接',runs_on:'运行于',
 ph_sql:'写 SQL，Cmd/Ctrl+Enter 执行',ph_sql_first:'选左侧连接后写 SQL，Cmd/Ctrl+Enter 执行',
 ph_redis:'redis 命令，如 GET key / SCAN 0 / HGETALL key',
 run:'运行',fmt:'格式化',hist:'历史',ro_badge:'只读 · 自动 LIMIT',
 check_health:'检查所有连接可用性',toggle_theme:'切换主题',switch_lang:'Switch to English',
 drag_editor:'拖拽调整编辑器高度',explain_title:'查看执行计划(EXPLAIN)',
 empty_grid:'选一个连接和表，或写条 SQL。',rows:'行',truncated:'已截断到上限',
 row_detail:'整行详情',cell:'单元格 · 点外部关闭 ·',copy:'复制',copied:'已复制',
 saved_queries:'已存查询',other:'其他',params_suffix:'参数',fill_params:'填参数',
 required:'*必填',default_v:'默认',
 filter_tables:'过滤表…',filter_keys:'过滤 key…',no_tables:'无表',no_keys:'无 key',
 running:'执行中…',hist_title:'查询历史',hist_search:'搜索历史…（SQL / 连接名）',
 no_match:'无匹配',no_hist:'暂无历史',new_query:'新查询',close_tab:'关闭标签',new_tab:'新标签',
 just_now:'刚刚',min_ago:' 分钟前',hr_ago:' 小时前',day_ago:' 天前',
 no_plan_redis:'redis 无执行计划',pick_conn:'先选一个连接',empty_plan:'（空计划）',
 prod_no_autorun:'已切到 prod，不自动执行 — 请手动点运行',
 copy_fail:'复制失败 — 剪贴板不可用',max_rows:'单次查询最大行数',
 keys_capped:'仅显示前 {n} 个 key — 请用过滤缩小范围',
 list_capped:'仅显示前 {n} 张表 — 请用过滤缩小范围',
 refresh_list:'刷新列表',alt_insert:'Alt+点击仅插入 SQL 不执行'}};
let LANG=localStorage.getItem('qy_lang')||'en';
const t=k=>(I18N[LANG]&&I18N[LANG][k])||I18N.en[k]||k;
const j=(u,o)=>fetch(u,o).then(async r=>{let d;try{d=await r.json();}catch(_){d={error:'bad response ('+r.status+')'};}
  if(!r.ok)throw d;return d;},e=>{throw{error:String((e&&e.message)||e)};});   // network failures -> readable {error}
let cur={db:null,env:null,engine:null,isRedis:false,table:null}, lastRes=null, TREE=null, selTd=null;
const QMETA={}, TCACHE={}, HEALTH={};
function setHealth(db,ok,err){HEALTH[db]=ok;$$('#side .dbrow').forEach(x=>{if(x.dataset.db===db){x.classList.toggle('down',!ok);x.title=ok?'':(err||'unreachable');const d=x.querySelector('.dot');if(d){d.classList.remove('ok','down','chk');d.classList.add(ok?'ok':'down');}}});}
async function checkHealth(){
  const btn=$('#healthBtn');btn.innerHTML='<i class="ti ti-loader"></i>';btn.classList.add('spin');
  $$('#side .dbrow .dot').forEach(d=>{if(!d.classList.contains('ok')&&!d.classList.contains('down'))d.classList.add('chk');});
  const items=[];for(const g of TREE)for(const it of g.items)items.push(it);
  let i=0;
  const worker=async()=>{ while(i<items.length){ const it=items[i++];
    const env=((it.envs.find(e=>e.env==='dev')||it.envs[0]||{}).env)||'';
    await fetch(`/api/health?db=${encodeURIComponent(it.db)}&env=${encodeURIComponent(env)}&fresh=1`)
      .then(r=>r.json()).then(d=>setHealth(it.db,!!d.ok,d.error)).catch(()=>setHealth(it.db,false));
  }};
  await Promise.all(Array.from({length:3},worker));   // cap concurrency at 3 (SSH races skew results)
  btn.classList.remove('spin');btn.innerHTML='<i class="ti ti-activity"></i>';
}
function loadHealthCache(){   // paint health dots from the backend cache (no probing, instant)
  for(const g of TREE) for(const it of g.items){
    const env=((it.envs.find(e=>e.env==='dev')||it.envs[0]||{}).env)||'';
    fetch(`/api/health?db=${encodeURIComponent(it.db)}&env=${encodeURIComponent(env)}&cached=1`)
      .then(r=>r.json()).then(d=>{if(d.ok===true||d.ok===false)setHealth(it.db,d.ok,d.error);}).catch(()=>{});
  }
}
const HIST=JSON.parse(localStorage.getItem('qy_hist')||'[]'); let hi=-1;

/* theme */
const themeBtn=$('#themeBtn');
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.setItem('qy_theme',t);
  themeBtn.innerHTML=t==='dark'?'<i class="ti ti-sun"></i>':'<i class="ti ti-moon"></i>';}
setTheme(localStorage.getItem('qy_theme')||'dark');
themeBtn.onclick=()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');

/* language: apply the active language to static chrome; toggle reloads */
(function(){
  document.documentElement.lang=LANG==='zh'?'zh':'en';
  $('#roBadge').innerHTML='<i class="ti ti-lock"></i> '+t('ro_badge');
  $('#healthBtn').title=t('check_health'); themeBtn.title=t('toggle_theme');
  $('#qtitle').textContent=t('no_conn'); $('#runon').textContent=t('runs_on');
  $('#edresizer').title=t('drag_editor'); $('#expBtn').title=t('explain_title');
  $('#runLbl').textContent=t('run'); $('#fmtLbl').textContent=t('fmt'); $('#histLbl').textContent=t('hist');
  $('#side').innerHTML='<div class="spin"><i class="ti ti-loader"></i> '+t('loading')+'</div>';
  $('#grid').innerHTML='<div class="empty">'+t('empty_grid')+'</div>';
  $('#sql').placeholder=t('ph_sql_first');
  const lb=$('#langBtn');
  lb.textContent=LANG==='en'?'中':'EN'; lb.title=t('switch_lang');
  lb.onclick=()=>{localStorage.setItem('qy_lang',LANG==='en'?'zh':'en');location.reload();};
  $('#maxRows').title=t('max_rows');
  [['#healthBtn','check_health'],['#themeBtn','toggle_theme'],['#langBtn','switch_lang'],['#expBtn','explain_title'],['#maxRows','max_rows']]
    .forEach(([sel,k])=>$(sel).setAttribute('aria-label',t(k)));
})();

/* max-rows cap: persisted, sent with every /api/query and /api/run */
const mrSel=$('#maxRows');
mrSel.value=localStorage.getItem('qy_maxrows')||'500';
mrSel.onchange=()=>localStorage.setItem('qy_maxrows',mrSel.value);
const maxRowsNow=()=>+(mrSel.value)||500;

/* sidebar resize */
(function(){const rz=$('#resizer'),aside=$('#side');const w=localStorage.getItem('qy_sw');if(w)aside.style.width=w+'px';
  rz.onmousedown=e=>{e.preventDefault();const sx=e.pageX,sw=aside.offsetWidth;
    const mv=ev=>{const nw=Math.min(480,Math.max(150,sw+ev.pageX-sx));aside.style.width=nw+'px';localStorage.setItem('qy_sw',nw);};
    const up=()=>{document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);};
    document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);};})();

/* editor resize (full-width bar, like the sidebar divider) */
(function(){const rz=$('#edresizer'),ed=document.querySelector('.edwrap');const h=localStorage.getItem('qy_edh');if(h)ed.style.height=h+'px';
  rz.onmousedown=e=>{e.preventDefault();const sy=e.pageY,sh=ed.offsetHeight;
    const mv=ev=>{const nh=Math.min(window.innerHeight*0.7,Math.max(70,sh+ev.pageY-sy));ed.style.height=nh+'px';localStorage.setItem('qy_edh',Math.round(nh));};
    const up=()=>{document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);};
    document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);};})();

/* SQL highlight overlay */
const KW=/\b(select|from|where|and|or|not|in|is|null|order|by|group|having|limit|offset|join|left|right|inner|outer|on|as|distinct|count|sum|avg|min|max|insert|update|delete|set|values|into|create|drop|alter|table|with|case|when|then|else|end|asc|desc|between|like|ilike|union|all)\b/gi;
function highlight(t){
  let h=esc(t);
  h=h.replace(/(--[^\n]*)/g,'<span class="tok-cm">$1</span>');
  h=h.replace(/('(?:[^']|'')*')/g,'<span class="tok-str">$1</span>');
  h=h.replace(/\b(\d+\.?\d*)\b/g,'<span class="tok-num">$1</span>');
  h=h.replace(KW,'<span class="tok-kw">$&</span>');
  return h+'\n';
}
const ta=$('#sql'), hl=$('#hl');
function syncHL(){hl.innerHTML=highlight(ta.value);hl.scrollTop=ta.scrollTop;}
ta.addEventListener('input',()=>{syncHL();saveUI();acUpdate();}); ta.addEventListener('scroll',()=>{hl.scrollTop=ta.scrollTop;acClose();});
ta.addEventListener('blur',()=>setTimeout(acClose,120));
ta.addEventListener('keydown',e=>{   // capture: handle AC nav before run/history handlers
  if(!acOpen()||e.metaKey||e.ctrlKey)return;   // let Cmd/Ctrl combos (run, history) through
  if(e.key==='ArrowDown'){e.preventDefault();e.stopPropagation();acMove(1);}
  else if(e.key==='ArrowUp'){e.preventDefault();e.stopPropagation();acMove(-1);}
  else if(e.key==='Enter'||e.key==='Tab'){e.preventDefault();e.stopPropagation();acAccept();}
  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();acClose();}
},true);
function setSQL(v){ta.value=v;syncHL();acClose();if(typeof saveUI==='function')saveUI();}
/* Preserve a hand-written draft before the editor is overwritten (table click,
   redis-key inspect, saved query, history recall) — recoverable from History. */
function keepDraft(next){const s=ta.value.trim();if(s&&s!==String(next||'').trim())pushHist(s);}

/* ---- editor tabs: each tab = {sql, db, env}, persisted across restarts.
   TABRES holds each tab's last result; it is persisted to `qy_tabres` (index-
   aligned with TABS) so EVERY tab's grid — not just the active one — survives a
   reload. The grid always reflects the active tab: switching tabs must never
   show (or export) another tab's data. ---- */
let TABS=[], ATI=0, TABRES=[], TID=0;
/* Each tab gets a stable, session-unique id so an in-flight request can be
   routed back to the tab that started it even after tabs are switched, closed,
   or reordered (index alone is unsafe: closing a lower tab shifts indices). */
function newTid(){return 't'+(++TID);}
let TABREQ={};   // tab id -> latest issued request seq (per-tab latest-wins)
(function(){
  try{TABS=JSON.parse(localStorage.getItem('qy_tabs')||'null')||[];}catch(e){TABS=[];}
  if(!TABS.length){                      // migrate from the old single-state key
    let ui=null; try{ui=JSON.parse(localStorage.getItem('qy_ui')||'null');}catch(e){}
    TABS=[{sql:(ui&&ui.sql)||'',db:(ui&&ui.db)||null,env:(ui&&ui.env)||null}];
  }
  TABS.forEach(tb=>{const n=+String(tb.id||'').slice(1);if(n>TID)TID=n;});  // avoid id reuse across reloads
  TABS.forEach(tb=>{if(!tb.id)tb.id=newTid();});
  ATI=Math.min(+(localStorage.getItem('qy_ati')||0)||0,TABS.length-1);
})();
function keepTid(i){const tb=TABS[i];return (tb&&tb.id)||newTid();}
function tabTitle(tb){return tb.db?(tb.db+(tb.env?'@'+tb.env:'')):((tb.sql||'').trim().split(/\s+/).slice(0,2).join(' ')||t('new_query'));}
function saveUI(){
  TABS[ATI]={id:keepTid(ATI),sql:ta.value,db:cur.db,env:cur.env};
  try{localStorage.setItem('qy_tabs',JSON.stringify(TABS));localStorage.setItem('qy_ati',String(ATI));}catch(e){}
  saveTabres();
  renderTabs();
}
/* Persist every tab's last result, index-aligned with TABS, tagged with the
   connection that PRODUCED the result (r._db/_env), not the tab's current
   connection — so re-pointing a tab to another db never mislabels an old grid,
   and reload's match check rejects it. On quota overflow, degrade to keeping
   only the active tab's result. */
function saveTabres(){
  const pack=i=>{const r=TABRES[i];if(!r)return null;const {_orig,_db,_env,...clean}=r;
    const tb=TABS[i]||{};
    return {db:(_db!==undefined?_db:tb.db),env:(_env!==undefined?_env:tb.env),res:clean};};
  try{localStorage.setItem('qy_tabres',JSON.stringify(TABS.map((tb,i)=>pack(i))));}
  catch(e){
    try{const arr=TABS.map(()=>null);arr[ATI]=pack(ATI);localStorage.setItem('qy_tabres',JSON.stringify(arr));}
    catch(e2){try{localStorage.removeItem('qy_tabres');}catch(e3){}}
  }
}
function renderTabs(){
  const bar=$('#tabs'); if(!bar)return;
  bar.innerHTML=TABS.map((tb,i)=>`<span class="tab${i===ATI?' on':''}" data-i="${i}" title="${esc((tb.sql||'').slice(0,300))}">${esc(tabTitle(tb))}${TABS.length>1?`<span class="x" data-x="${i}" title="${t('close_tab')}">×</span>`:''}</span>`).join('')
    +`<span class="tab add" id="tabAdd" title="${t('new_tab')}">+</span>`;
  bar.querySelectorAll('.tab[data-i]').forEach(el=>el.onclick=e=>{
    if(e.target.dataset.x!==undefined){e.stopPropagation();closeTab(+e.target.dataset.x);return;}
    switchTab(+el.dataset.i);});
  const add=bar.querySelector('#tabAdd');
  if(add)add.onclick=()=>{TABS[ATI]={id:keepTid(ATI),sql:ta.value,db:cur.db,env:cur.env};TABS.push({id:newTid(),sql:'',db:cur.db,env:cur.env});switchTab(TABS.length-1);};
}
function showTabResult(){                     // grid + status always reflect the ACTIVE tab
  const r=TABRES[ATI], tb=TABS[ATI]||{};
  // only restore a grid whose producing connection still matches the tab — a
  // rebound / vanished connection must never show another connection's rows
  if(r && r._db===tb.db && (r._env||null)===(tb.env||null)){sortState={i:-1,dir:1};render(r);}
  else{lastRes=null;selTd=null;$('#status').style.display='none';
       $('#grid').innerHTML='<div class="empty">'+t('empty_grid')+'</div>';}
}
function loadTab(tb){                         // NB: param must not shadow the i18n fn t()
  ta.value=tb.sql||''; syncHL(); acClose();
  if(tb.db&&TREE&&TREE.some(g=>g.items.some(x=>x.db===tb.db))) selectDb(tb.db,tb.env||null);
  else{                                       // no / vanished connection: unbind — never silently
    tb.db=null; tb.env=null;                  // rebind the tab to whatever was selected before
    cur={db:null,env:null,engine:null,isRedis:false,table:null};
    $$('#side .dbrow').forEach(x=>x.classList.remove('on'));
    $('#tbl-panel')?.remove();
    $('#qtitle').textContent=t('no_conn'); $('#esw').innerHTML='';
    $('#runon').style.display='none'; $('#prodBadge').style.display='none';
    saveUI();
  }
  showTabResult();
}
function switchTab(i){
  if(i===ATI&&TABS[i]){renderTabs();return;}
  TABS[ATI]={id:keepTid(ATI),sql:ta.value,db:cur.db,env:cur.env};
  ATI=i; loadTab(TABS[i]); ta.focus();
}
function closeTab(i){
  const dying=(i===ATI?ta.value:(TABS[i]&&TABS[i].sql)||'').trim();
  if(dying)pushHist(dying);                   // closing a tab must never silently lose SQL
  const wasActive=i===ATI;
  TABS.splice(i,1); TABRES.splice(i,1);
  if(!TABS.length)TABS=[{id:newTid(),sql:'',db:cur.db,env:cur.env}];
  if(i<ATI)ATI--;
  ATI=Math.min(ATI,TABS.length-1);
  if(wasActive)loadTab(TABS[ATI]); else saveUI();
}

/* ---- SQL autocomplete: local keywords + tables + columns (no network beyond /api/columns) ---- */
const AC_KW='select from where and or not in is null order by group having limit offset join left right inner outer on as distinct count sum avg min max coalesce with case when then else end asc desc between like ilike union all insert update delete set values into'.split(' ');
const COLS={};                               // table -> [colnames], lazily filled from /api/columns
let acItems=[], acIndex=0, acTok=null, acBox=null;
function acTablesNow(){const d=TCACHE[cur.db+'@'+(cur.env||'')];return (d&&d.tables)||[];}
function fetchCols(table){
  if(COLS[table]!==undefined)return; COLS[table]=[];   // [] marks in-flight (avoids refetch)
  fetch(`/api/columns?db=${encodeURIComponent(cur.db)}&env=${encodeURIComponent(cur.env||'')}&table=${encodeURIComponent(table)}`)
    .then(r=>r.json()).then(d=>{COLS[table]=d.columns||[];acUpdate();}).catch(()=>{});
}
function caretXY(){
  const s=getComputedStyle(ta), m=document.createElement('div');
  ['fontFamily','fontSize','fontWeight','lineHeight','letterSpacing','paddingTop','paddingRight','paddingBottom','paddingLeft'].forEach(p=>m.style[p]=s[p]);
  m.style.cssText+=';position:absolute;visibility:hidden;box-sizing:border-box;white-space:pre-wrap;word-break:break-word;top:0;left:-9999px';
  m.style.width=ta.clientWidth+'px';
  m.textContent=ta.value.slice(0,ta.selectionStart);
  const cur2=document.createElement('span'); cur2.textContent='​'; m.appendChild(cur2);
  document.body.appendChild(m);
  const x=cur2.offsetLeft, y=cur2.offsetTop, lh=parseFloat(s.lineHeight)||16; m.remove();
  const r=ta.getBoundingClientRect();
  return {x:r.left+x-ta.scrollLeft, y:r.top+y-ta.scrollTop+lh};
}
function acOpen(){return acBox&&acBox.style.display==='block'&&acItems.length;}
function acClose(){if(acBox)acBox.style.display='none';acItems=[];}
function acShow(items,from,to){
  if(!items.length){acClose();return;}
  acItems=items;acIndex=0;acTok={from,to};
  if(!acBox){acBox=document.createElement('div');acBox.className='acbox';document.body.appendChild(acBox);
    acBox.onmousedown=e=>{e.preventDefault();const el=e.target.closest('[data-i]');if(el)acAccept(+el.dataset.i);};}
  acBox.innerHTML=items.map((it,i)=>`<div class="acitem${i===0?' on':''}" data-i="${i}"><span class="ack ack-${it.k}">${it.k}</span>${esc(it.t)}</div>`).join('');
  const c=caretXY(); acBox.style.left=Math.round(c.x)+'px'; acBox.style.top=Math.round(c.y)+'px'; acBox.style.display='block';
}
function acMove(d){if(!acOpen())return;acIndex=(acIndex+d+acItems.length)%acItems.length;
  [...acBox.children].forEach((el,i)=>el.classList.toggle('on',i===acIndex));
  acBox.children[acIndex]?.scrollIntoView({block:'nearest'});}
function acAccept(i){
  if(!acOpen())return; const it=acItems[i==null?acIndex:i]; if(!it){acClose();return;}
  const v=ta.value, np=acTok.from+it.t.length;
  ta.value=v.slice(0,acTok.from)+it.t+v.slice(acTok.to);
  ta.selectionStart=ta.selectionEnd=np; syncHL(); saveUI(); acClose(); ta.focus();
}
function acUpdate(){
  if(!cur.db||cur.isRedis||document.activeElement!==ta){acClose();return;}
  const pos=ta.selectionStart, pre=ta.value.slice(0,pos); let m;
  if(m=pre.match(/([A-Za-z_][\w$]*)\.([\w$]*)$/)){        // table.col
    const tbl=m[1], frag=m[2].toLowerCase(); fetchCols(tbl);
    const cands=(COLS[tbl]||[]).filter(c=>c.toLowerCase().startsWith(frag)).map(t=>({t,k:'col'}));
    acShow(cands.slice(0,12),pos-m[2].length,pos); return;
  }
  if(m=pre.match(/([A-Za-z_][\w$]*)$/)){                  // bare word
    const frag=m[1], lf=frag.toLowerCase();
    const prevKw=(pre.slice(0,pre.length-frag.length).match(/([A-Za-z_]\w*)\s+$/)||[])[1]||'';
    const tbls=acTablesNow().filter(t=>t.toLowerCase().startsWith(lf)).map(t=>({t,k:'tbl'}));
    let list=tbls;
    if(!/^(from|join|into|update)$/i.test(prevKw)){       // after FROM/JOIN -> tables only
      const rc=(lastRes?lastRes.columns.map(c=>c.name):[]).filter(c=>c.toLowerCase().startsWith(lf)).map(t=>({t,k:'col'}));
      const kw=AC_KW.filter(k=>k.startsWith(lf)).map(k=>({t:k.toUpperCase(),k:'kw'}));
      list=tbls.concat(rc,kw);
    }
    const seen=new Set(), out=[];
    for(const it of list){const key=it.k+':'+it.t.toLowerCase();if(seen.has(key))continue;seen.add(key);out.push(it);if(out.length>=12)break;}
    if(out.length===1&&out[0].t.toLowerCase()===lf){acClose();return;}   // already fully typed
    acShow(out,pos-frag.length,pos); return;
  }
  acClose();
}

/* sidebar */
async function loadSide(){
  const data=await j('/api/connections'); TREE=data.groups;
  const wss=data.workspaces||[data.workspace];
  $('#ws').innerHTML=wss.length>1
    ? `<i class="ti ti-stack-2"></i> ${wss.length} workspaces`
    : '<i class="ti ti-folder"></i> '+esc(data.workspace);
  $('#ws').title=wss.join('\n');
  let html='';
  for(const g of TREE){
    const orig=g.ws?g.ws.split('/').slice(-2).join('/'):'';
    const gkey=(g.ws||'')+'::'+(g.group||t('other'));
    html+=`<div class="grp" data-grp data-gkey="${esc(gkey)}"><i class="ti ti-chevron-down"></i> ${esc(g.group||t('other'))}${orig?`<span class="wsorig" title="${esc(g.ws)}">${esc(orig)}</span>`:''}</div><div class="gbody">`;
    for(const it of g.items){
      const rc=it.engine==='redis'?' redis':'';
      html+=`<div class="dbrow${rc}" data-db="${esc(it.db)}"><span class="dot"></span>${esc(it.db)}<small>${esc(it.engine)}</small></div>`;
      if(it.envs.length>1){
        const defEnv=(it.envs.find(e=>e.env==='dev')||it.envs[0]).env;
        html+='<div class="pills">'+it.envs.map(e=>`<span class="pill${e.env===defEnv?' on':''}${e.env==='prod'?' prod':''}" data-db="${esc(it.db)}" data-env="${esc(e.env||'')}">${esc(e.env||'default')}</span>`).join('')+'</div>';
      }
    }
    html+='</div>';
  }
  const qs=await j('/api/queries'); qs.forEach(q=>QMETA[q.name]=q);
  if(qs.length){html+=`<div class="grp" data-grp data-gkey="__saved__"><i class="ti ti-chevron-down"></i> ${t('saved_queries')}</div><div class="gbody">`;
    html+=qs.map(q=>{const pb=q.params.length?`<span class="rbadge">${q.params.length} ${t('params_suffix')}</span>`:'';
      return `<div class="qname" data-q="${esc(q.name)}" title="${esc(q.desc||q.name)}"><i class="ti ti-bookmark"></i>${esc(q.name)}${pb}</div>`;}).join('')+'</div>';}
  $('#side').innerHTML=html;
  const collapsed=new Set(JSON.parse(localStorage.getItem('qy_collapsed')||'[]'));
  $$('#side [data-grp]').forEach(el=>{
    const key=el.dataset.gkey;
    if(collapsed.has(key)){el.nextElementSibling.style.display='none';el.querySelector('.ti').className='ti ti-chevron-right';}
    el.onclick=()=>{const b=el.nextElementSibling;const c=b.style.display==='none';b.style.display=c?'':'none';
      el.querySelector('.ti').className=c?'ti ti-chevron-down':'ti ti-chevron-right';
      if(c)collapsed.delete(key);else collapsed.add(key);
      localStorage.setItem('qy_collapsed',JSON.stringify([...collapsed]));};
  });
  $$('#side .dbrow').forEach(el=>el.onclick=()=>selectDb(el.dataset.db,null));
  $$('#side .pill').forEach(el=>el.onclick=ev=>{ev.stopPropagation();selectDb(el.dataset.db,el.dataset.env||null,{via:true});});
  $$('#side .qname').forEach(el=>el.onclick=()=>openSaved(el.dataset.q));
  Object.keys(HEALTH).forEach(db=>setHealth(db,HEALTH[db]));   // restore known health from this session
  loadHealthCache();                                            // backend cache (survives reloads)
  try{                                             // restore every tab's result, then the active tab
    let tr=null; try{tr=JSON.parse(localStorage.getItem('qy_tabres')||'null');}catch(e){}
    if(Array.isArray(tr)){
      TABRES=TABS.map((tb,i)=>{const e=tr[i];
        if(e&&e.res&&e.db===tb.db&&(e.env||null)===(tb.env||null)){e.res._db=e.db;e.res._env=e.env||null;return e.res;}
        return undefined;});
    }else{                                          // migrate the old single-result key
      const lr=JSON.parse(localStorage.getItem('qy_result')||'null');
      const tb=TABS[ATI]; if(lr&&lr.res&&tb&&lr.db===tb.db&&(lr.env||null)===(tb.env||null)){lr.res._db=lr.db;lr.res._env=lr.env||null;TABRES[ATI]=lr.res;}
    }
    const tab=TABS[ATI]||{};
    if(tab.sql)setSQL(tab.sql);
    if(tab.db && TREE.some(g=>g.items.some(i=>i.db===tab.db))) selectDb(tab.db,tab.env||null);
    cur.db=cur.db||tab.db;cur.env=cur.env||tab.env;
    showTabResult();   // only paints TABRES[ATI] if its producing connection still matches the tab
    renderTabs();
  }catch(e){}
}
function openSaved(name){
  const q=QMETA[name]; if(!q)return;
  const nv=q.sql||('-- '+name); keepDraft(nv); setSQL(nv);
  if(!q.params.length){runSaved(name,{});return;}
  const m=document.createElement('div');m.className='modal';
  const fields=q.params.map(p=>{const req=p.required?` <span style="color:var(--red-fg)">${t('required')}</span>`:(p.default!=null?` <span style="color:var(--fg3)">${t('default_v')} ${esc(p.default)}</span>`:'');
    return `<div style="margin:8px 0"><label style="font-size:12px;color:var(--fg2);display:block;margin-bottom:3px">${esc(p.name)} <span style="color:var(--fg3)">${esc(p.type||'text')}</span>${req}</label>
      <input class="pf" data-p="${esc(p.name)}" value="${esc(p.default!=null?p.default:'')}" placeholder="${esc(p.name)}" style="width:100%;background:var(--bg2);border:1px solid var(--line2);border-radius:6px;color:var(--fg);padding:6px 9px;font-family:var(--mono);font-size:12.5px"></div>`;}).join('');
  m.innerHTML=`<div class="box" style="width:min(460px,80%)"><div class="mh"><i class="ti ti-adjustments"></i> ${esc(name)} · ${t('fill_params')}</div>
    ${q.desc?`<div style="color:var(--fg3);font-size:11.5px;margin-bottom:6px">${esc(q.desc)}</div>`:''}
    ${fields}
    <div style="text-align:right;margin-top:12px"><button class="btn primary" id="pgo"><i class="ti ti-player-play"></i> ${t('run')}</button></div></div>`;
  m.onclick=e=>{if(e.target===m)m.remove();};
  const go=()=>{const params={};m.querySelectorAll('.pf').forEach(i=>{if(i.value!=='')params[i.dataset.p]=i.value;});m.remove();runSaved(name,params);};
  m.querySelector('#pgo').onclick=go;
  document.body.appendChild(m);
  const first=m.querySelector('.pf'); if(first){first.focus();first.select();}
  m.addEventListener('keydown',e=>{if(e.key==='Enter')go();});
}

async function selectDb(db,env,opt={}){
  // re-click active db (no env change) toggles its table panel
  if(cur.db===db && env===null && !opt.via){const p=$('#tbl-panel'); if(p){p.style.display=p.style.display==='none'?'':'none'; return;}}
  if(cur.db!==db)cur.table=null;
  cur.db=db; cur.env=env;
  $$('#side .dbrow').forEach(x=>x.classList.toggle('on',x.dataset.db===db));
  $('#qtitle').textContent=db;
  renderEnvSwitcher(db,env);
  $$('#side .pill').forEach(p=>{if(p.dataset.db===db)p.classList.toggle('on',p.dataset.env===(cur.env||''));});
  saveUI();
  await renderTables(db,cur.env);
  if(opt.via && ta.value.trim() && !cur.isRedis){        // env switch -> re-run current SQL…
    if((cur.env||'').toLowerCase()==='prod') toast(t('prod_no_autorun'),true);  // …but never auto-run on prod
    else run();
  }
}
function renderEnvSwitcher(db,env){
  let item=null; for(const g of TREE) for(const it of g.items) if(it.db===db) item=it;
  cur.engine=item?item.engine:null; cur.isRedis=cur.engine==='redis';
  const envs=item?item.envs:[]; const multi=envs.length>1;
  if(multi){
    if(!env) env=(envs.find(e=>e.env==='dev')||envs[0]).env; cur.env=env;
    $('#runon').style.display='';
    $('#esw').innerHTML=envs.map(e=>`<span class="ep${e.env===cur.env?' on':''}${e.env==='prod'?' prod':''}" data-env="${esc(e.env||'')}">${esc(e.env||'default')}</span>`).join('');
    $$('#esw .ep').forEach(x=>x.onclick=()=>selectDb(db,x.dataset.env,{via:true}));
  } else {
    cur.env=env||(envs[0]?envs[0].env:null); $('#runon').style.display='none'; $('#esw').innerHTML='';
  }
  $('#prodBadge').style.display=(cur.env||'').toLowerCase()==='prod'?'':'none';
  ta.placeholder=cur.isRedis?t('ph_redis'):t('ph_sql');
}
async function renderTables(db,env,fresh){
  const old=$('#tbl-panel');                    // keep the filter text across panel rebuilds (same db)
  const prevQ=(old&&old.dataset.db===db&&old.querySelector('.tsearch'))?old.querySelector('.tsearch').value:'';
  old?.remove();
  const active=$('#side .dbrow.on'); if(!active) return;
  const panel=document.createElement('div'); panel.id='tbl-panel'; panel.dataset.db=db;
  let anchor=active.nextElementSibling;
  while(anchor && anchor.classList.contains('pills')) anchor=anchor.nextElementSibling;
  active.parentNode.insertBefore(panel,anchor);
  const key=db+'@'+(env||'');
  const paint=data=>{                            // repaint keeps the user's filter text + applies it
    const q=(panel.querySelector('.tsearch')||{}).value||prevQ;
    cur.isRedis?renderRedisPanel(panel,data.keys||[],data.capped):renderTablePanel(panel,data.tables||[],data.capped);
    if(q){const s=panel.querySelector('.tsearch');if(s){s.value=q;s.oninput();}}};
  if(!fresh && TCACHE[key]) paint(TCACHE[key]);   // 缓存优先:秒出（手动刷新则直连 fresh）
  else panel.innerHTML='<div class="spin" style="padding:8px"><i class="ti ti-loader"></i></div>';
  const qp=`db=${encodeURIComponent(db)}&env=${encodeURIComponent(env||'')}${fresh?'&fresh=1':''}`;
  try{
    const data=await j(`/api/tables?${qp}`);   // 命中后端缓存则秒回
    TCACHE[key]=data; setHealth(db,true);
    if($('#tbl-panel')===panel) paint(data);
    if(data._cached){                            // SWR:是缓存 → 后台 fresh 拉一次替换
      j(`/api/tables?${qp}&fresh=1`).then(fd=>{TCACHE[key]=fd;if($('#tbl-panel')===panel)paint(fd);}).catch(()=>{});
    }
  }catch(e){ setHealth(db,false,e.error||String(e)); if(!TCACHE[key] && $('#tbl-panel')===panel) panel.innerHTML='<div class="empty">'+esc(e.error||e)+'</div>'; }
}
function qid(t){ if(/^[a-z_][a-z0-9_$]*$/.test(t)) return t;   // quote mixed-case / reserved identifiers
  return cur.engine==='mysql' ? '`'+t.replace(/`/g,'``')+'`' : '"'+t.replace(/"/g,'""')+'"'; }
function renderTablePanel(panel, tables, capped){
  const note=capped?`<div class="hmeta" style="padding:0 12px 5px">${esc(t('list_capped').replace('{n}',tables.length))}</div>`:'';
  panel.innerHTML=`<div class="trow"><input class="tsearch" placeholder="${t('filter_tables')}"><button class="treload" title="${t('refresh_list')}"><i class="ti ti-refresh"></i></button></div>`+note+(tables.length
    ?tables.map(tb=>`<div class="tname${tb===cur.table?' on':''}" data-t="${esc(tb)}" title="${esc(tb)}&#10;${t('alt_insert')}"><i class="ti ti-table"></i>${esc(tb)}</div>`).join('')
    :`<div class="empty">${t('no_tables')}</div>`);
  panel.querySelectorAll('.tname').forEach(el=>el.onclick=ev=>{
    const q='select * from '+qid(el.dataset.t)+' limit 100';
    keepDraft(q);setSQL(q);
    if(ev.altKey){ta.focus();return;}                            // alt+click: insert only, don't run
    cur.table=el.dataset.t;
    panel.querySelectorAll('.tname').forEach(x=>x.classList.toggle('on',x===el));
    run();
  });
  const rb=panel.querySelector('.treload'); if(rb)rb.onclick=()=>renderTables(cur.db,cur.env,true);
  const s=panel.querySelector('.tsearch'); s.oninput=()=>{const q=s.value.toLowerCase();
    panel.querySelectorAll('.tname').forEach(el=>el.style.display=el.dataset.t.toLowerCase().includes(q)?'':'none');};
}
function fmtTtl(s){return s>86400?Math.round(s/86400)+'d':s>3600?Math.round(s/3600)+'h':s>60?Math.round(s/60)+'m':s+'s';}
function kTree(keys){const root={dirs:{},leaves:[]};
  for(const k of keys){const parts=k.key.split(':');let n=root;
    for(let i=0;i<parts.length-1;i++){const seg=parts[i];n.dirs[seg]=n.dirs[seg]||{dirs:{},leaves:[],name:seg};n=n.dirs[seg];}
    n.leaves.push({...k,label:parts[parts.length-1]||k.key});}
  return root;}
function kCount(n){let c=n.leaves.length;for(const d in n.dirs)c+=kCount(n.dirs[d]);return c;}
function renderKNode(node){let h='';
  for(const name in node.dirs){const d=node.dirs[name];
    h+=`<div class="tname knode"><i class="ti ti-chevron-down"></i>${esc(name)}<span class="rbadge">${kCount(d)}</span></div><div class="kchild">${renderKNode(d)}</div>`;}
  for(const lf of node.leaves){const ttl=lf.ttl>0?`<span class="rbadge ttl">${fmtTtl(lf.ttl)}</span>`:'';
    h+=`<div class="tname" data-key="${esc(lf.key)}" title="${esc(lf.key)}"><i class="ti ti-key"></i>${esc(lf.label)}<span class="rbadge">${esc(lf.type)}</span>${ttl}</div>`;}
  return h;}
function renderRedisPanel(panel, keys, capped){
  const note=capped?`<div class="hmeta" style="padding:0 12px 5px">${esc(t('keys_capped').replace('{n}',keys.length))}</div>`:'';
  panel.innerHTML=`<div class="trow"><input class="tsearch" placeholder="${t('filter_keys')}"><button class="treload" title="${t('refresh_list')}"><i class="ti ti-refresh"></i></button></div>`+note+`<div id="ktree"></div>`;
  const rb=panel.querySelector('.treload'); if(rb)rb.onclick=()=>renderTables(cur.db,cur.env,true);
  const draw=list=>{$('#ktree').innerHTML=list.length?renderKNode(kTree(list)):`<div class="empty">${t('no_keys')}</div>`;
    panel.querySelectorAll('.knode').forEach(el=>el.onclick=()=>{const c=el.nextElementSibling;const h=c.style.display==='none';
      c.style.display=h?'':'none';el.querySelector('.ti').className=h?'ti ti-chevron-down':'ti ti-chevron-right';});
    panel.querySelectorAll('#ktree .tname[data-key]').forEach(el=>el.onclick=()=>inspectKey(el.dataset.key));};
  draw(keys);
  const s=panel.querySelector('.tsearch'); s.oninput=()=>{const q=s.value.toLowerCase();draw(keys.filter(k=>k.key.toLowerCase().includes(q)));};
}

/* run — overlapping requests are latest-wins PER TAB, and every response is
   routed back to the tab (by stable id) and connection that issued it:
   - a stale (earlier) response never overwrites a newer one in the same tab;
   - a response that lands after the user switched tabs is stored on ITS OWN
     tab (never the now-active one) and only painted if that tab is still active;
   - a response whose tab was closed is dropped. */
let runSeq=0;
function loading(){$('#grid').innerHTML='<div class="spin"><i class="ti ti-loader"></i> '+t('running')+'</div>';}
function startReq(){                           // snapshot the issuing tab + connection
  const tid=keepTid(ATI); const seq=++runSeq; TABREQ[tid]=seq;
  return {tid,seq,db:cur.db,env:cur.env};
}
function fresh(ctx,res){                       // apply a fresh result to its origin tab
  if(TABREQ[ctx.tid]!==ctx.seq)return;         // superseded by a newer request in that tab
  const idx=TABS.findIndex(t=>t.id===ctx.tid);
  if(idx<0)return;                             // tab was closed while in flight
  const tb=TABS[idx]||{};                      // the tab may have been re-pointed to another
  if(tb.db!==ctx.db||(tb.env||null)!==(ctx.env||null))return;  // connection while in flight -> drop, never mislabel
  res._db=ctx.db; res._env=ctx.env;            // tag with the connection that produced it
  if(idx===ATI){sortState={i:-1,dir:1};render(res);}   // render() stores TABRES[ATI] + persists
  else{TABRES[idx]=res;saveTabres();}          // background tab: store + persist, never touch grid
}
function failReq(ctx,e){                        // an error only affects the still-active issuing tab
  if(TABREQ[ctx.tid]!==ctx.seq)return;
  const idx=TABS.findIndex(t=>t.id===ctx.tid);
  if(idx===ATI)showErr(e);
}
async function run(){
  if(!cur.db)return; const sql=ta.value.trim(); if(!sql)return;
  if(cur.table&&sql!=='select * from '+qid(cur.table)+' limit 100'){    // custom SQL -> grid no longer shows that table
    cur.table=null;$$('#tbl-panel .tname.on').forEach(x=>x.classList.remove('on'));}
  acClose(); pushHist(sql); loading();
  const ctx=startReq();
  try{ const res=await j('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({db:ctx.db,env:ctx.env,sql,maxRows:maxRowsNow()})});
    fresh(ctx,res); }
  catch(e){ failReq(ctx,e); }
}
async function inspectKey(key){
  keepDraft('# '+key); setSQL('# '+key); loading();
  const ctx=startReq();
  try{ const res=await j(`/api/inspect?db=${encodeURIComponent(ctx.db)}&env=${encodeURIComponent(ctx.env||'')}&key=${encodeURIComponent(key)}`);
    fresh(ctx,res); }
  catch(e){ failReq(ctx,e); }
}
async function runSaved(name,params){
  loading();
  const ctx=startReq();
  try{ const res=await j('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,env:ctx.env,params:params||{},maxRows:maxRowsNow()})});
    if(TABREQ[ctx.tid]!==ctx.seq)return;
    if(TABS.findIndex(t=>t.id===ctx.tid)===ATI)setSQL(res.sql);   // only rewrite the editor if still on that tab
    fresh(ctx,res); }
  catch(e){ failReq(ctx,e); }
}
function showErr(e){$('#status').style.display='none';$('#grid').innerHTML='<div class="err">'+esc(e.error||JSON.stringify(e))+'</div>';}

/* grid */
function cellText(v){ if(v===null||v===undefined)return null;
  if(typeof v==='object')return JSON.stringify(v);   // jsonb / arrays
  return String(v); }
function cellClass(v){ if(v===null||v===undefined)return 'null';
  if(typeof v==='number')return 'num';
  if(typeof v==='object')return 'json';
  const s=String(v);
  if(/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(s))return 'uuid';
  if(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:/.test(s))return 'ts';
  if(s==='true'||s==='false')return 'bool';
  if(/^-?\d+\.?\d*$/.test(s))return 'num';
  return ''; }
let sortState={i:-1,dir:1};
function render(res){
  lastRes=res; selTd=null; TABRES[ATI]=res;   // result belongs to the active tab
  saveTabres();
  const cols=res.columns;
  let st=`<span><span class="cu">${res.rowCount}</span> ${t('rows')}</span><span><i class="ti ti-clock"></i> ${res.elapsedMs} ms</span>`;
  if(res.truncated)st+=`<span class="tr"><i class="ti ti-arrow-narrow-down"></i> ${t('truncated')}</span>`;
  st+=`<span style="flex:1"></span><span>${esc(cur.db)}${cur.env?'@'+esc(cur.env):''} · ${esc(res.engine)}</span>`;
  $('#status').style.display='';$('#status').innerHTML=st;
  if(!res.rows.length){$('#grid').innerHTML=`<div class="empty">0 ${t('rows')}</div>`;return;}
  const head='<tr><th class="rownum">#</th>'+cols.map((c,i)=>{
    const ar=sortState.i===i?`<span class="ar">${sortState.dir>0?'↑':'↓'}</span>`:'';
    return `<th data-i="${i}">${esc(c.name)}${c.type?`<span class="ty">${esc(c.type)}</span>`:''}${ar}<span class="rz"></span></th>`;}).join('')+'</tr>';
  const body=res.rows.map((r,ri)=>{
    let tds=`<td class="rownum" data-ri="${ri}" title="${t('row_detail')}">${ri+1}</td>`;
    tds+=cols.map(c=>{const v=r[c.name];const cl=cellClass(v);const t=cellText(v);
      const disp=t===null?'NULL':esc(t);
      return `<td class="${cl}" data-v="${esc(t===null?'':t)}" title="${esc(t===null?'NULL':t)}">${disp}</td>`;}).join('');
    return '<tr>'+tds+'</tr>';}).join('');
  $('#grid').innerHTML=`<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
  wireGrid();
}
function wireGrid(){
  $$('#grid td:not(.rownum)').forEach(td=>{
    td.onclick=()=>{if(selTd)selTd.classList.remove('sel');selTd=td;td.classList.add('sel');};
    td.ondblclick=()=>{const v=td.dataset.v;(v.length>60||/^[\[{]/.test(v))?openModal(v):copy(v);};
  });
  $$('#grid td.rownum').forEach(td=>td.onclick=()=>rowDetail(lastRes.rows[+td.dataset.ri]));
  $$('#grid th[data-i]').forEach(th=>{th.onclick=e=>{if(e.target.classList.contains('rz'))return;sortBy(+th.dataset.i);};
    const rz=th.querySelector('.rz'); if(rz)rz.onmousedown=e=>startResize(e,th);});
}
function sortBy(i){ if(!lastRes)return;
  if(sortState.i===i&&sortState.dir<0){                       // 3rd click on the same column -> original order
    if(lastRes._orig)lastRes.rows=lastRes._orig.slice();
    sortState={i:-1,dir:1}; render(lastRes); return;
  }
  if(!lastRes._orig)lastRes._orig=lastRes.rows.slice();       // snapshot pre-sort order once per result
  sortState.dir=sortState.i===i?-sortState.dir:1; sortState.i=i;
  const col=lastRes.columns[i].name;
  const numish=v=>typeof v==='number'||(typeof v==='string'&&v.trim()!==''&&!isNaN(v));  // '10' > '9', not '10' < '9'
  lastRes.rows.sort((a,b)=>{let x=a[col],y=b[col];if(x===null||x===undefined)return 1;if(y===null||y===undefined)return -1;
    if(numish(x)&&numish(y))return (Number(x)-Number(y))*sortState.dir;
    return String(x).localeCompare(String(y))*sortState.dir;});
  render(lastRes);
}
function startResize(e,th){e.preventDefault();e.stopPropagation();const sx=e.pageX,sw=th.offsetWidth;
  const mv=ev=>{th.style.width=Math.max(50,sw+ev.pageX-sx)+'px';th.style.minWidth=th.style.width;};
  const up=()=>{document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);};
  document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);}
function jsonTree(v,key){
  const k=key!==undefined?`<span class="jk">${esc(key)}</span>: `:'';
  if(v===null)return `<div class="jrow">${k}<span class="jnull">null</span></div>`;
  if(Array.isArray(v)){
    if(!v.length)return `<div class="jrow">${k}[]</div>`;
    return `<details class="jt" open><summary>${k}<span class="jm">[${v.length}]</span></summary>${v.map((x,i)=>jsonTree(x,i)).join('')}</details>`;
  }
  if(typeof v==='object'){
    const ks=Object.keys(v);
    if(!ks.length)return `<div class="jrow">${k}{}</div>`;
    return `<details class="jt" open><summary>${k}<span class="jm">{${ks.length}}</span></summary>${ks.map(kk=>jsonTree(v[kk],kk)).join('')}</details>`;
  }
  const cls=typeof v==='number'?'jnum':typeof v==='boolean'?'jbool':'jstr';
  return `<div class="jrow">${k}<span class="${cls}">${esc(typeof v==='string'?JSON.stringify(v):String(v))}</span></div>`;
}
function openModal(v){
  let body=null;                                   // JSON -> collapsible tree
  try{const p=JSON.parse(v);if(p&&typeof p==='object')body=jsonTree(p);}catch(_){}
  if(body===null)body=`<pre>${esc(v)}</pre>`;
  const m=document.createElement('div');m.className='modal';
  m.innerHTML=`<div class="box" style="min-width:min(560px,80vw)"><div class="mh"><i class="ti ti-eye"></i> ${t('cell')} <span id="cpy" style="cursor:pointer;color:var(--accent)">${t('copy')}</span></div>${body}</div>`;
  m.onclick=e=>{if(e.target===m)m.remove();};
  m.querySelector('#cpy').onclick=()=>{copy(v);m.remove();};
  document.body.appendChild(m);}
function rowDetail(row){
  const m=document.createElement('div');m.className='modal';
  const rows=lastRes.columns.map(c=>{const t=cellText(row[c.name]);const disp=t===null?'<span style="color:var(--null);font-style:italic">NULL</span>':esc(t);
    return `<tr><td style="color:var(--fg2);padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap">${esc(c.name)}${c.type?` <span class="ty" style="color:var(--fg3)">${esc(c.type)}</span>`:''}</td><td style="padding:4px 0;word-break:break-word;font-family:var(--mono)">${disp}</td></tr>`;}).join('');
  m.innerHTML=`<div class="box" style="width:60%"><div class="mh"><i class="ti ti-list-details"></i> ${t('row_detail')}</div><table style="border:0;width:100%">${rows}</table></div>`;
  m.onclick=e=>{if(e.target===m)m.remove();};document.body.appendChild(m);}
function copy(v){const s=String(v);   // only claim "Copied" when the clipboard write actually succeeded
  if(navigator.clipboard&&navigator.clipboard.writeText)
    navigator.clipboard.writeText(s).then(()=>toast(t('copied'),true),()=>toast(t('copy_fail'),false));
  else toast(t('copy_fail'),false);}

/* export */
function toCSV(res){const cols=res.columns.map(c=>c.name);
  const esc2=v=>{const s=cellText(v);if(s===null)return '';return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;};
  return [cols.join(','),...res.rows.map(r=>cols.map(c=>esc2(r[c])).join(','))].join('\n');}
function download(name,text,type){const b=new Blob([text],{type});const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download=name;a.click();}
const expName=ext=>`quarry-${cur.db||'export'}.${ext}`;
$('#csvBtn').onclick=()=>{if(lastRes)download(expName('csv'),'\ufeff'+toCSV(lastRes),'text/csv;charset=utf-8');};   // BOM: Excel-safe UTF-8
$('#jsonBtn').onclick=()=>{if(lastRes)download(expName('json'),JSON.stringify(lastRes.rows,null,2),'application/json');};

/* format (light) */
$('#fmtBtn').onclick=()=>{let s=ta.value;
  s=s.replace(/\s+/g,' ').replace(/\s*,\s*/g,', ').trim();
  s=s.replace(/\b(select|from|where|order by|group by|having|limit|offset|left join|right join|inner join|join|on|and|or|union|values|insert into|update|set|delete from)\b/gi,m=>m.toUpperCase());
  s=s.replace(/\b(FROM|WHERE|ORDER BY|GROUP BY|HAVING|LIMIT|OFFSET|LEFT JOIN|RIGHT JOIN|INNER JOIN|JOIN|UNION)\b/g,'\n$1');
  setSQL(s);};

/* history — entries are {sql, db, env, ts} (older versions stored bare strings) */
const hSql=h=>typeof h==='string'?h:(h.sql||'');
function fmtAgo(ts){if(!ts)return '';const s=(Date.now()-ts)/1000;
  return s<60?t('just_now'):s<3600?Math.floor(s/60)+t('min_ago'):s<86400?Math.floor(s/3600)+t('hr_ago'):Math.floor(s/86400)+t('day_ago');}
function pushHist(sql){if(hSql(HIST[0]||'')!==sql){HIST.unshift({sql,db:cur.db,env:cur.env,ts:Date.now()});
  HIST.length=Math.min(HIST.length,100);
  localStorage.setItem('qy_hist',JSON.stringify(HIST));}hi=-1;}
$('#histBtn').onclick=()=>{if(!HIST.length)return toast(t('no_hist'),true);
  const m=document.createElement('div');m.className='modal';
  const row=(h,i)=>{const o=typeof h==='string'?{sql:h}:h;
    const meta=[o.db?o.db+(o.env?'@'+o.env:''):'',fmtAgo(o.ts)].filter(Boolean).join(' · ');
    return `<div class="hitem" data-i="${i}" style="cursor:pointer;padding:7px 6px;border-bottom:1px solid var(--line)">
      <pre style="margin:0;font-family:var(--mono);font-size:12.5px;white-space:pre-wrap;word-break:break-word">${esc(o.sql)}</pre>
      ${meta?`<div class="hmeta">${esc(meta)}</div>`:''}</div>`;};
  m.innerHTML=`<div class="box" style="width:min(680px,80%)"><div class="mh"><i class="ti ti-history"></i> ${t('hist_title')} · ${HIST.length}</div>
    <input class="hsearch" placeholder="${t('hist_search')}"><div id="hlist">${HIST.map(row).join('')}</div></div>`;
  m.onclick=e=>{if(e.target===m)m.remove();};
  document.body.appendChild(m);
  const wire=()=>m.querySelectorAll('.hitem').forEach(el=>el.onclick=()=>{const v=hSql(HIST[+el.dataset.i]);keepDraft(v);setSQL(v);m.remove();ta.focus();});
  wire();
  const s=m.querySelector('.hsearch'); s.focus();
  s.oninput=()=>{const q=s.value.toLowerCase();
    m.querySelector('#hlist').innerHTML=HIST.map((h,i)=>({h,i})).filter(({h})=>{
      const o=typeof h==='string'?{sql:h}:h;
      return o.sql.toLowerCase().includes(q)||String(o.db||'').toLowerCase().includes(q);
    }).map(({h,i})=>row(h,i)).join('')||`<div class="empty">${t('no_match')}</div>`;
    wire();};};

/* toast */
let tt; function toast(msg,ok){clearTimeout(tt);let el=$('#toast');if(!el){el=document.createElement('div');el.id='toast';el.className='toast';document.body.appendChild(el);}
  el.style.background=ok?'var(--ok-bg)':'var(--red-bg)';el.style.color=ok?'var(--ok)':'var(--red-fg)';el.style.borderColor=ok?'var(--ok)':'var(--red)';
  el.textContent=msg;el.style.display='';tt=setTimeout(()=>el.style.display='none',ok?1400:4000);}

$('#runBtn').onclick=run;
$('#healthBtn').onclick=checkHealth;
/* EXPLAIN: run the current SQL under EXPLAIN; single-column plans open in a
   modal (postgres), tabular plans (mysql) render in the grid. */
$('#expBtn').onclick=async()=>{
  if(!cur.db||cur.isRedis)return toast(cur.isRedis?t('no_plan_redis'):t('pick_conn'),false);
  const sql=ta.value.trim(); if(!sql)return;
  const btn=$('#expBtn'); btn.disabled=true; const ctx=startReq();
  try{
    const res=await j('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({db:ctx.db,env:ctx.env,sql:'EXPLAIN '+sql.replace(/^\s*explain\s+/i,'')})});
    if(res.columns.length>1){fresh(ctx,res);return;}
    if(TABREQ[ctx.tid]!==ctx.seq)return;                 // superseded by a newer request in this tab
    const idx=TABS.findIndex(t=>t.id===ctx.tid); const tb=TABS[idx]||{};
    if(idx!==ATI||tb.db!==ctx.db||(tb.env||null)!==(ctx.env||null))return;  // tab switched / re-pointed in flight
    const col=res.columns[0]?res.columns[0].name:null;
    const plan=col?res.rows.map(r=>r[col]).join('\n'):t('empty_plan');
    const m=document.createElement('div');m.className='modal';
    m.innerHTML=`<div class="box" style="min-width:min(760px,85vw)"><div class="mh"><i class="ti ti-route"></i> EXPLAIN · ${esc(ctx.db)}${ctx.env?'@'+esc(ctx.env):''}</div><pre>${esc(plan)}</pre></div>`;
    m.onclick=e=>{if(e.target===m)m.remove();};document.body.appendChild(m);
  }catch(e){toast(e.error||String(e),false);}
  finally{btn.disabled=false;}
};
let draftStash='';   // in-flight draft while Cmd+Up/Down walks history; restored at the bottom
ta.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();run();}
  if((e.metaKey||e.ctrlKey)&&e.key==='ArrowUp'){e.preventDefault();if(hi<HIST.length-1){if(hi===-1)draftStash=ta.value;hi++;setSQL(hSql(HIST[hi]));}}
  if((e.metaKey||e.ctrlKey)&&e.key==='ArrowDown'){e.preventDefault();if(hi>0){hi--;setSQL(hSql(HIST[hi]));}else if(hi===0){hi=-1;setSQL(draftStash);}}
});
/* grid keyboard navigation: arrows move the selected cell, Enter inspects */
function moveSel(dr,dc){
  if(!selTd)return;
  const tr=selTd.parentElement, tbody=tr.parentElement;
  const nr=tbody.rows[tr.sectionRowIndex+dr]; if(!nr)return;
  const ci=Math.min(Math.max(selTd.cellIndex+dc,1),nr.cells.length-1);
  const nc=nr.cells[ci]; if(!nc)return;
  selTd.classList.remove('sel'); selTd=nc; nc.classList.add('sel');
  nc.scrollIntoView({block:'nearest',inline:'nearest'});
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){const ms=$$('.modal');if(ms.length){ms[ms.length-1].remove();return;}}   // close the topmost modal
  if((e.metaKey||e.ctrlKey)&&e.key==='c'&&selTd&&document.activeElement!==ta&&!String(getSelection())){
    copy(selTd.dataset.v);}
  const typing=/INPUT|TEXTAREA/.test((document.activeElement||{}).tagName||'');
  if(selTd&&!typing&&!e.metaKey&&!e.ctrlKey&&!e.altKey&&!$('.modal')){
    if(e.key==='ArrowDown'){e.preventDefault();moveSel(1,0);}
    else if(e.key==='ArrowUp'){e.preventDefault();moveSel(-1,0);}
    else if(e.key==='ArrowLeft'){e.preventDefault();moveSel(0,-1);}
    else if(e.key==='ArrowRight'){e.preventDefault();moveSel(0,1);}
    else if(e.key==='Enter'){e.preventDefault();openModal(selTd.dataset.v);}
  }
});
loadSide();
</script>
</body></html>"""


if __name__ == "__main__":
    sys.exit(main())
