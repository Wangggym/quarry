"""SSH tunnel support — reach a DB that's only bound to the bastion's localhost.

Zero-dependency: shells out to the system `ssh` binary (`ssh -L`). A connection
grows optional fields:  ssh_host, ssh_user, ssh_key, ssh_port.

Tunnels are POOLED (keyed by ssh target + db host:port) and reused across calls,
so a long-running GUI opens each tunnel once instead of per query. Pooled
tunnels are torn down at process exit. Failures are fast: a missing key file
errors immediately, and if `ssh` exits early we surface its stderr rather than
waiting out the full timeout.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse, urlunparse

from . import proxy as proxy_mod
from . import workspace

DEFAULT_DB_PORT = {"postgres": 5432, "mysql": 3306, "redis": 6379}

_POOL: dict[tuple, "_Tunnel"] = {}
_LOCK = threading.Lock()


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
        local_port = t.local_port
    yield _rewrite_url_hostport(conn.url, "127.0.0.1", local_port)


def close_all() -> None:
    with _LOCK:
        for t in _POOL.values():
            try:
                t.proc.terminate()
            except Exception:
                pass
        _POOL.clear()


atexit.register(close_all)
