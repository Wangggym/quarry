"""v2 unit tests: SSH-tunnel URL rewrite + Redis engine safety/parse (no network)."""

from __future__ import annotations

import pytest

from quarry import redis_engine
from quarry.tunnel import _db_host_port, _rewrite_url_hostport


# ---- tunnel URL rewrite ----

def test_rewrite_keeps_user_and_pass():
    url = "postgresql://postgres:secret@127.0.0.1:5432/appdb"
    out = _rewrite_url_hostport(url, "127.0.0.1", 54321)
    assert out == "postgresql://postgres:secret@127.0.0.1:54321/appdb"


def test_rewrite_password_only_userinfo():
    # redis://:pw@host has empty username — password must survive the rewrite.
    url = "redis://:wuGgh@127.0.0.1:6379/0"
    out = _rewrite_url_hostport(url, "127.0.0.1", 16000)
    assert out == "redis://:wuGgh@127.0.0.1:16000/0"


def test_db_host_port_defaults():
    assert _db_host_port("redis://:pw@10.0.0.1/0", "redis") == ("10.0.0.1", 6379)
    assert _db_host_port("postgresql://u@h/db", "postgres") == ("h", 5432)


# ---- redis safety ----

@pytest.mark.parametrize("cmd", ["GET k", "SCAN 0", "TYPE k", "TTL k", "HGETALL h",
                                  "LRANGE l 0 -1", "DBSIZE", "KEYS *", "EXISTS k", "INFO"])
def test_redis_reads_allowed(cmd):
    assert redis_engine.is_redis_read_only(cmd) is True


@pytest.mark.parametrize("cmd", ["SET k v", "DEL k", "FLUSHALL", "EXPIRE k 10",
                                  "HSET h f v", "LPUSH l x", "CONFIG SET x y", "RENAME a b"])
def test_redis_writes_blocked(cmd):
    assert redis_engine.is_redis_read_only(cmd) is False


# ---- redis url parse ----

def test_parse_redis_url():
    cfg = redis_engine.parse_redis_url("redis://:pw@1.2.3.4:6380/3")
    assert cfg == {"host": "1.2.3.4", "port": 6380, "password": "pw", "db": "3"}


def test_parse_redis_url_defaults():
    cfg = redis_engine.parse_redis_url("redis://localhost")
    assert cfg["port"] == 6379 and cfg["db"] == "0" and cfg["password"] is None
