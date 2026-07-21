"""SSH tunnel support — reach a DB that's only bound to the bastion's localhost.

Zero-dependency: shells out to the system `ssh` binary (`ssh -L`). A connection
grows optional fields:  ssh_host, ssh_user, ssh_key, ssh_port.

Tunnels are POOLED (keyed by ssh target + db host:port) and reused across calls,
so a long-running GUI opens each tunnel once instead of per query. Pooled
tunnels are torn down at process exit. Failures are fast: a missing key file
errors immediately, and if `ssh` exits early we surface its stderr rather than
waiting out the full timeout.

Each process's pool lives only in that process's memory, so a live tunnel is
also mirrored to a small on-disk registry (~/.cache/quarry/tunnels.json) —
this is what lets a separately-invoked `qy proxy` (or the GUI's per-connection
proxy badge) see tunnels a different, long-running `qy gui`/MCP process is
holding open, as an observed fact rather than an empty list (issue #101).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import proxy as proxy_mod
from . import workspace

DEFAULT_DB_PORT = {"postgres": 5432, "mysql": 3306, "redis": 6379}

_POOL: dict[tuple, "_Tunnel"] = {}
_LOCK = threading.Lock()


def _default_registry_file() -> Path:
    # QUARRY_TUNNEL_REGISTRY_FILE lets a test point at an isolated file —
    # same override pattern as cache.py's QUARRY_CACHE_FILE.
    override = os.environ.get("QUARRY_TUNNEL_REGISTRY_FILE")
    return Path(override).expanduser() if override else Path.home() / ".cache" / "quarry" / "tunnels.json"


REGISTRY_FILE = _default_registry_file()

# Registry keys this process itself wrote (issue #101 r1-1): `_POOL` only
# ever holds tunnels *this* process spawned, so this process is the only one
# allowed to remove their registry entries (on stale-dimension replacement or
# at exit) — a long-running `qy gui`/MCP process's entries must survive a
# separately-invoked `qy proxy` reading (and garbage-collecting dead entries
# from) the same file.
_OWNED_REGISTRY_KEYS: set[str] = set()


class _Tunnel:
    def __init__(self, proc: subprocess.Popen, local_port: int):
        self.proc = proc
        self.local_port = local_port

    def alive(self) -> bool:
        return self.proc.poll() is None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(host: str, port: int, proc: subprocess.Popen, timeout: float = 9.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:   # ssh exited early — no point waiting
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _db_host_port(url: str, engine: str) -> tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"//{url}", scheme=engine)
    return (parsed.hostname or "127.0.0.1", parsed.port or DEFAULT_DB_PORT.get(engine, 5432))


def _port_open(host: str, port: int) -> bool:
    """A quick, best-effort liveness probe for a tunnel this process doesn't
    hold the subprocess handle for (a registry entry written by another
    process) — same 0.3s-probe style as proxy.py's `_port_listening`."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _registry_key(key: tuple) -> str:
    ssh_host, ssh_port, ssh_user, ssh_key, db_host, db_port, proxy_key = key
    proxy_part = f"{proxy_key[0]}:{proxy_key[1]}" if proxy_key else "-"
    return f"{ssh_user}@{ssh_host}:{ssh_port}|{ssh_key or '-'}|{db_host}:{db_port}|{proxy_part}"


def _load_registry() -> dict:
    try:
        with REGISTRY_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_registry(data: dict) -> None:
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, REGISTRY_FILE)
    except Exception:
        pass


def _register_tunnel(key: tuple, t: "_Tunnel", proxy_key) -> None:
    """Persist a freshly-established tunnel to the cross-process registry
    (issue #101 r1-1): `_POOL` is only ever visible within this process, so
    without this, a separately-invoked `qy proxy` (a brand-new process with
    its own empty `_POOL`) can never observe a tunnel a long-running `qy
    gui`/MCP process is holding open — `qy proxy` would report an empty
    tunnel list even while queries are actively flowing through one.
    Must be called with `_LOCK` already held."""
    ssh_host, ssh_port, ssh_user, _ssh_key, db_host, db_port, _pk = key
    rkey = _registry_key(key)
    registry = _load_registry()
    registry[rkey] = {
        "ssh_target": f"{ssh_user}@{ssh_host}:{ssh_port}",
        "db_target": f"{db_host}:{db_port}",
        "local_port": t.local_port,
        "proxied": proxy_key is not None,
        "proxy": f"{proxy_key[0]}:{proxy_key[1]}" if proxy_key else None,
    }
    _save_registry(registry)
    _OWNED_REGISTRY_KEYS.add(rkey)


def _rewrite_url_hostport(url: str, new_host: str, new_port: int) -> str:
    parsed = urlparse(url)
    userinfo = ""
    if parsed.username or parsed.password:   # handle password-only (redis://:pw@host)
        userinfo = parsed.username or ""
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    return urlunparse(parsed._replace(netloc=f"{userinfo}{new_host}:{new_port}"))


def _proxy_command_option(proxy_info: "proxy_mod.ProxyInfo") -> str:
    """`-o ProxyCommand=...` value: ssh substitutes %h/%p with the ssh target it
    is actually connecting to (the bastion), so proxycommand.py doesn't need to
    know it ahead of time. `sys.executable` (not a bare `python`) because a
    GUI/launchd-spawned process's PATH is often too thin to find one."""
    return (f"{shlex.quote(sys.executable)} -m quarry.proxycommand "
            f"{shlex.quote(proxy_info.host)} {proxy_info.port} %h %p")


def _make_tunnel(
    conn, db_host: str, db_port: int, connect_timeout: float | None = None,
    proxy_info: "proxy_mod.ProxyInfo | None" = None,
) -> _Tunnel:
    """`connect_timeout=None` keeps the historical fixed budget (ssh
    ConnectTimeout=6, port-wait up to 9s) used by short probes (connections
    test, describe-table, health checks — untouched by issue #94). A given
    value (query paths pass DEFAULT_CONNECT_TIMEOUT_SEC) drives both.

    `proxy_info`, when set, routes the ssh TCP stream through it via
    `ProxyCommand` (see proxycommand.py) — the fix for issue #96's throttled
    cross-border ssh tunnels."""
    from .core import EXIT_CONNECTION_ERROR, QuarryError

    key_path = os.path.expanduser(conn.ssh_key) if getattr(conn, "ssh_key", None) else None
    if key_path and not os.path.exists(key_path):
        raise QuarryError(
            f"ssh key not found: {key_path} — install the bastion key (or fix ssh_key)",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    local_port = _free_port()
    wait_timeout = connect_timeout if connect_timeout is not None else 9.0
    ssh_connect_timeout = max(1, int(connect_timeout)) if connect_timeout is not None else 6
    cmd = [
        "ssh", "-N",
        "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"ConnectTimeout={ssh_connect_timeout}",
        "-o", "ServerAliveInterval=15",
    ]
    if proxy_info is not None:
        cmd += ["-o", f"ProxyCommand={_proxy_command_option(proxy_info)}"]
    cmd += [
        "-L", f"127.0.0.1:{local_port}:{db_host}:{db_port}",
        "-p", str(getattr(conn, "ssh_port", None) or 22),
    ]
    if key_path:
        cmd += ["-i", key_path]
    cmd += [f"{getattr(conn, 'ssh_user', None) or 'root'}@{conn.ssh_host}"]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not _wait_port("127.0.0.1", local_port, proc, timeout=wait_timeout):
        stderr = b""
        try:
            proc.terminate()
            _, stderr = proc.communicate(timeout=3)
        except Exception:
            pass
        detail = (stderr.decode("utf-8", "replace").strip() or "port not ready / timeout")[:300]
        raise QuarryError(
            f"ssh tunnel to {conn.ssh_host} failed: {detail}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return _Tunnel(proc, local_port)


@contextlib.contextmanager
def open_tunnel(conn, engine: str, connect_timeout: float | None = None, use_proxy: bool | None = None):
    """Yield an effective DB URL. If conn has ssh_host, ensure a pooled tunnel and
    yield a localhost URL; otherwise yield conn.url unchanged.

    `connect_timeout` bounds tunnel establishment (issue #94: connection setup is
    capped independently of, and more tightly than, query execution) — it only
    matters on the first call for a given ssh target; a pooled/reused tunnel
    returns immediately. Callers that don't pass it (existing short probes) keep
    the historical fixed budget — see `_make_tunnel`.

    `use_proxy` (issue #96): None defers to the owning workspace's persisted
    proxy toggle (`qy proxy on|off`); False forces a direct connection for this
    call only (CLI `--no-proxy`). Only affects connections with `ssh_host` — a
    direct (non-tunneled) DB connection can't be routed through an HTTP proxy,
    see `check_connection_write`'s warning at `connections add` time."""
    if not getattr(conn, "ssh_host", None):
        yield conn.url
        return

    db_host, db_port = _db_host_port(conn.url, engine)
    ws_home = getattr(conn, "source", None) or workspace.WS.home
    proxy_info = proxy_mod.should_use_proxy(conn.ssh_host, workspace_home=ws_home, override=use_proxy)
    proxy_key = (proxy_info.host, proxy_info.port) if proxy_info is not None else None
    key = (conn.ssh_host, getattr(conn, "ssh_port", None) or 22,
           getattr(conn, "ssh_user", None) or "root", getattr(conn, "ssh_key", None) or "",
           db_host, db_port, proxy_key)
    with _LOCK:
        t = _POOL.get(key)
        if t is not None and not t.alive():
            t = None
        if t is None:
            t = _make_tunnel(conn, db_host, db_port, connect_timeout=connect_timeout, proxy_info=proxy_info)
            _POOL[key] = t
            _terminate_stale_dimension(key)
            _register_tunnel(key, t, proxy_key)
        local_port = t.local_port
    yield _rewrite_url_hostport(conn.url, "127.0.0.1", local_port)


def _terminate_stale_dimension(new_key: tuple) -> None:
    """After pooling a freshly-established tunnel for `new_key`, terminate and
    drop any other pooled tunnel for the same (ssh target, db target) but a
    different proxy dimension (issue #101). Flipping the workspace's proxy
    toggle changes every subsequent pool key (see the `proxy_key` component
    above), so without this the old tunnel's ssh process just keeps running
    until the process exits — an idle zombie that `qy proxy`'s tunnel listing
    would otherwise expose as two tunnels to the same target at once.

    Must be called with `_LOCK` already held."""
    prefix = new_key[:-1]
    stale_keys = [k for k in _POOL if k[:-1] == prefix and k != new_key]
    if not stale_keys:
        return
    registry = _load_registry()
    changed = False
    for stale_key in stale_keys:
        stale = _POOL.pop(stale_key)
        try:
            stale.proc.terminate()
        except Exception:
            pass
        rkey = _registry_key(stale_key)
        _OWNED_REGISTRY_KEYS.discard(rkey)
        if registry.pop(rkey, None) is not None:
            changed = True
    if changed:
        _save_registry(registry)


def list_tunnels() -> list[dict]:
    """Snapshot of every tunnel currently reachable, in-process or not
    (issue #101 r1-1): `qy proxy` and the GUI need to answer "is this
    connection actually going through the proxy right now, and is the tunnel
    still alive?" as an observed fact — but `qy proxy` runs as its own fresh
    process, so a tunnel a long-running `qy gui`/MCP process is holding open
    lives entirely in *that* process's `_POOL`, invisible here without the
    shared registry file. Entries from other processes have their liveness
    re-checked by probing the recorded local port (we don't hold their
    subprocess handle to poll()); dead ones are pruned from the registry as
    they're noticed."""
    with _LOCK:
        items = []
        seen_rkeys = set()
        for key, t in _POOL.items():
            ssh_host, ssh_port, ssh_user, _ssh_key, db_host, db_port, proxy_key = key
            items.append({
                "ssh_target": f"{ssh_user}@{ssh_host}:{ssh_port}",
                "db_target": f"{db_host}:{db_port}",
                "local_port": t.local_port,
                "proxied": proxy_key is not None,
                "proxy": f"{proxy_key[0]}:{proxy_key[1]}" if proxy_key else None,
                "alive": t.alive(),
            })
            seen_rkeys.add(_registry_key(key))
        registry = _load_registry()
    stale_rkeys = []
    for rkey, entry in registry.items():
        if rkey in seen_rkeys:
            continue
        if not _port_open("127.0.0.1", entry["local_port"]):
            stale_rkeys.append(rkey)
            continue
        items.append({**entry, "alive": True})
    if stale_rkeys:
        for rkey in stale_rkeys:
            registry.pop(rkey, None)
        _save_registry(registry)
    return items


def tunnel_fact_for(conn, engine: str) -> dict | None:
    """The currently-live `list_tunnels()` entry for `conn`, if any (issue
    #101 r1-2) — for the GUI's per-connection proxy badge to report what a
    tunnel is actually doing right now instead of predicting what a fresh
    connection would do. Returns None when `conn` has no `ssh_host` (nothing
    to tunnel) or no tunnel currently exists for it (never queried yet, or
    the old tunnel from before a workspace proxy-toggle flip was already
    torn down by `_terminate_stale_dimension` and the replacement hasn't
    been created yet)."""
    if not getattr(conn, "ssh_host", None):
        return None
    db_host, db_port = _db_host_port(conn.url, engine)
    ssh_target = f"{getattr(conn, 'ssh_user', None) or 'root'}@{conn.ssh_host}:{getattr(conn, 'ssh_port', None) or 22}"
    db_target = f"{db_host}:{db_port}"
    for t in list_tunnels():
        if t["ssh_target"] == ssh_target and t["db_target"] == db_target:
            return t
    return None


def close_all() -> None:
    with _LOCK:
        for t in _POOL.values():
            try:
                t.proc.terminate()
            except Exception:
                pass
        _POOL.clear()
        if _OWNED_REGISTRY_KEYS:
            registry = _load_registry()
            for rkey in _OWNED_REGISTRY_KEYS:
                registry.pop(rkey, None)
            _save_registry(registry)
            _OWNED_REGISTRY_KEYS.clear()


atexit.register(close_all)
