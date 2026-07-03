"""Redis engine — talks to Redis via the system `redis-cli` (zero-dependency).

A redis "query" is a redis command string (e.g. `GET foo`, `SCAN 0 COUNT 50`,
`HGETALL bar`). Results map to single-column rows ({"value": ...}) so they flow
through the same QueryResult contract / GUI grid as SQL engines.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Any
from urllib.parse import unquote, urlparse

# Commands that mutate state — blocked unless allow_write=True.
_REDIS_WRITE = {
    "set", "setnx", "setex", "psetex", "mset", "msetnx", "append", "getset", "getdel", "getex",
    "del", "unlink", "expire", "pexpire", "expireat", "pexpireat", "persist", "rename", "renamenx",
    "incr", "decr", "incrby", "decrby", "incrbyfloat",
    "hset", "hsetnx", "hmset", "hincrby", "hincrbyfloat", "hdel",
    "hexpire", "hpexpire", "hexpireat", "hpexpireat", "hpersist", "hgetex", "hgetdel", "hsetex",
    "lpush", "rpush", "lpushx", "rpushx", "lpop", "rpop", "lset", "linsert", "lrem", "ltrim",
    "lmove", "blmove", "rpoplpush", "brpoplpush", "lmpop", "blmpop", "blpop", "brpop",
    "sadd", "srem", "spop", "smove", "sinterstore", "sunionstore", "sdiffstore",
    "zadd", "zincrby", "zrem", "zremrangebyrank", "zremrangebyscore", "zremrangebylex",
    "zpopmin", "zpopmax", "bzpopmin", "bzpopmax", "zmpop", "bzmpop",
    "zrangestore", "zdiffstore", "zinterstore", "zunionstore",
    "flushdb", "flushall", "swapdb", "move", "restore", "copy",
    "setbit", "setrange", "bitop", "pfadd", "pfmerge", "pfdebug", "geoadd", "geosearchstore",
    "xadd", "xdel", "xtrim", "xsetid", "xack", "xclaim", "xautoclaim", "xgroup", "xreadgroup",
    "config", "save", "bgsave", "bgrewriteaof", "shutdown", "slaveof", "replicaof", "failover",
    "subscribe", "publish", "spublish", "psubscribe", "monitor", "debug", "reset",
    "script", "eval", "evalsha", "eval_ro", "evalsha_ro", "fcall", "fcall_ro", "function",
    "acl", "client", "cluster", "slowlog", "latency", "flushslots",
}

# Commands that are reads by default but become writes when a subtoken appears.
_REDIS_COND_WRITE = {
    "sort": {"store"},
    "georadius": {"store", "storedist"},
    "georadiusbymember": {"store", "storedist"},
    "bitfield": {"set", "incrby", "overflow"},
}


def resolve_redis_cli() -> str:
    for cand in (os.environ.get("QUARRY_REDIS_CLI", "redis-cli"),
                 "/opt/homebrew/bin/redis-cli", "/usr/local/bin/redis-cli"):
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    from .core import EXIT_CONNECTION_ERROR, QuarryError
    raise QuarryError("redis-cli not found (set QUARRY_REDIS_CLI or install redis)",
                      exit_code=EXIT_CONNECTION_ERROR)


def parse_redis_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    db = parsed.path.lstrip("/") or "0"
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 6379,
        "password": unquote(parsed.password) if parsed.password else None,
        "db": db,
    }


def first_word(command: str) -> str:
    s = command.strip()
    if s.startswith("--"):  # e.g. `--scan`
        return s.split()[0].lstrip("-").lower()
    return (s.split() or [""])[0].lower()


def is_redis_read_only(command: str) -> bool:
    cmd = first_word(command)
    if cmd in _REDIS_WRITE:
        return False
    cond = _REDIS_COND_WRITE.get(cmd)
    if cond:
        try:
            rest = {a.lower() for a in shlex.split(command)[1:]}
        except ValueError:
            rest = {a.lower() for a in command.split()[1:]}
        if rest & cond:
            return False
    return True


def _cli_base(url: str) -> list[str]:
    cfg = parse_redis_url(url)
    cmd = [resolve_redis_cli(), "-h", cfg["host"], "-p", str(cfg["port"]), "-n", str(cfg["db"])]
    if cfg["password"]:
        cmd += ["-a", cfg["password"], "--no-auth-warning"]
    return cmd


def run_redis(url: str, command: str, *, timeout: int = 30) -> list[dict[str, Any]]:
    """Run a redis command, return rows. SCAN/KEYS/LRANGE/etc → one row per element."""
    argv = shlex.split(command)
    cmd = _cli_base(url) + argv
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        from .core import EXIT_CONNECTION_ERROR, QuarryError
        raise QuarryError(f"redis-cli timed out after {timeout}s", exit_code=EXIT_CONNECTION_ERROR)
    if proc.returncode != 0 or proc.stderr.strip():
        from .core import EXIT_SQL_ERROR, QuarryError
        raise QuarryError(f"redis error: {proc.stderr.strip() or proc.stdout.strip()}", exit_code=EXIT_SQL_ERROR)
    lines = [ln for ln in proc.stdout.splitlines()]
    # Trim a single trailing blank line redis-cli sometimes emits.
    while lines and lines[-1] == "":
        lines.pop()
    return [{"value": ln} for ln in lines]


def scan_keys(url: str, *, pattern: str = "*", count: int = 500) -> list[str]:
    cmd = _cli_base(url) + ["--scan", "--pattern", pattern, "--count", "100"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return []
    keys = [ln for ln in proc.stdout.splitlines() if ln]
    return keys[:count]


def keys_with_meta(url: str, *, pattern: str = "*", cap: int = 400) -> list[dict[str, Any]]:
    """Return [{key, type, ttl}] for up to `cap` keys (TYPE+TTL per key).

    Cheap for small keyspaces; capped so a huge DB can't stall the UI.
    """
    keys = scan_keys(url, pattern=pattern, count=cap)
    out: list[dict[str, Any]] = []
    for k in keys:
        try:
            t = run_redis(url, f"TYPE {shlex.quote(k)}", timeout=10)
            ttl = run_redis(url, f"TTL {shlex.quote(k)}", timeout=10)
            out.append({"key": k, "type": t[0]["value"] if t else "?",
                        "ttl": int(ttl[0]["value"]) if ttl else -1})
        except Exception:
            out.append({"key": k, "type": "?", "ttl": -1})
    return out


def inspect_key(url: str, key: str) -> list[dict[str, Any]]:
    """TYPE-aware read of a key, for the GUI 'click a key' flow."""
    t_rows = run_redis(url, f"TYPE {shlex.quote(key)}")
    ktype = t_rows[0]["value"] if t_rows else "none"
    reader = {
        "string": f"GET {shlex.quote(key)}",
        "hash": f"HGETALL {shlex.quote(key)}",
        "list": f"LRANGE {shlex.quote(key)} 0 -1",
        "set": f"SMEMBERS {shlex.quote(key)}",
        "zset": f"ZRANGE {shlex.quote(key)} 0 -1 WITHSCORES",
    }.get(ktype)
    if not reader:
        return [{"key": key, "type": ktype, "value": "(unsupported type)"}]
    rows = run_redis(url, reader)
    return [{"key": key, "type": ktype, "value": r["value"]} for r in rows]
