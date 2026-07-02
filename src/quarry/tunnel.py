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
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse, urlunparse

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


def _make_tunnel(conn, db_host: str, db_port: int) -> _Tunnel:
    from .core import EXIT_CONNECTION_ERROR, QuarryError

    key_path = os.path.expanduser(conn.ssh_key) if getattr(conn, "ssh_key", None) else None
    if key_path and not os.path.exists(key_path):
        raise QuarryError(
            f"ssh key not found: {key_path} — install the bastion key (or fix ssh_key)",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    local_port = _free_port()
    cmd = [
        "ssh", "-N",
        "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=6",
        "-o", "ServerAliveInterval=15",
        "-L", f"127.0.0.1:{local_port}:{db_host}:{db_port}",
        "-p", str(getattr(conn, "ssh_port", None) or 22),
    ]
    if key_path:
        cmd += ["-i", key_path]
    cmd += [f"{getattr(conn, 'ssh_user', None) or 'root'}@{conn.ssh_host}"]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not _wait_port("127.0.0.1", local_port, proc):
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
def open_tunnel(conn, engine: str):
    """Yield an effective DB URL. If conn has ssh_host, ensure a pooled tunnel and
    yield a localhost URL; otherwise yield conn.url unchanged."""
    if not getattr(conn, "ssh_host", None):
        yield conn.url
        return

    db_host, db_port = _db_host_port(conn.url, engine)
    key = (conn.ssh_host, getattr(conn, "ssh_port", None) or 22,
           getattr(conn, "ssh_user", None) or "root", getattr(conn, "ssh_key", None) or "",
           db_host, db_port)
    with _LOCK:
        t = _POOL.get(key)
        if t is not None and not t.alive():
            t = None
        if t is None:
            t = _make_tunnel(conn, db_host, db_port)
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
