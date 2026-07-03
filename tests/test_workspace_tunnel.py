"""Unit tests for quarry.workspace + quarry.tunnel.

Everything here is pure/mocked — no real DB, no network, and no real ssh.
Config-touching tests always relocate QUARRY_CONFIG under tmp_path so the
user's real ~/.config/quarry/config.toml is never read or written. Tunnel
tests monkeypatch subprocess.Popen / socket / _wait_port; no ssh is spawned.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from quarry import tunnel, workspace


# ---------------------------------------------------------------------------
# workspace.py — config discovery
# ---------------------------------------------------------------------------

def _use_config(monkeypatch, tmp_path: Path) -> Path:
    """Point QUARRY_CONFIG at a throwaway file; return its path."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("QUARRY_CONFIG", str(cfg))
    return cfg


@pytest.mark.unit
def test_config_path_honours_env(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    assert workspace._config_path() == cfg


@pytest.mark.unit
def test_config_workspaces_absent_file(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)  # file does not exist yet
    assert workspace.config_workspaces() == []
    assert workspace._dirs_from_config() == []


@pytest.mark.unit
def test_config_workspaces_valid_list(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    a = tmp_path / "wsa"
    b = tmp_path / "wsb"
    a.mkdir()
    b.mkdir()
    cfg.write_text(
        f'workspaces = ["{a}", "{b}"]\n', encoding="utf-8"
    )
    # raw (as written)
    assert workspace.config_workspaces() == [str(a), str(b)]
    # resolved Paths
    dirs = workspace._dirs_from_config()
    assert dirs == [a.resolve(), b.resolve()]


@pytest.mark.unit
def test_config_workspaces_malformed_toml_returns_empty(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text("workspaces = [this is not valid toml\n", encoding="utf-8")
    # no raise; both readers swallow the parse error
    assert workspace.config_workspaces() == []
    assert workspace._dirs_from_config() == []


@pytest.mark.unit
def test_config_workspaces_missing_key(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text('other = "value"\n', encoding="utf-8")
    assert workspace.config_workspaces() == []
    assert workspace._dirs_from_config() == []


@pytest.mark.unit
def test_config_workspaces_blank_entries_filtered_in_dirs(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    cfg.write_text(f'workspaces = ["{real}", "", "   "]\n', encoding="utf-8")
    # config_workspaces keeps everything raw
    assert workspace.config_workspaces() == [str(real), "", "   "]
    # _dirs_from_config filters blank/whitespace-only entries
    assert workspace._dirs_from_config() == [real.resolve()]


@pytest.mark.unit
def test_config_workspaces_string_instead_of_list_iterates_chars(monkeypatch, tmp_path):
    """CURRENT (documented) behavior: a bare string for `workspaces` is iterated
    char-by-char rather than treated as one path. There is no guard against it."""
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text('workspaces = "abc"\n', encoding="utf-8")
    # config_workspaces yields each character as its own "path"
    assert workspace.config_workspaces() == ["a", "b", "c"]
    # _dirs_from_config likewise resolves each single char against cwd
    dirs = workspace._dirs_from_config()
    assert dirs == [
        Path("a").expanduser().resolve(),
        Path("b").expanduser().resolve(),
        Path("c").expanduser().resolve(),
    ]


# ---------------------------------------------------------------------------
# workspace.py — add / remove / round-trip
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_add_workspace_new_returns_true_and_writes(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    d = tmp_path / "proj"
    d.mkdir()
    added, path = workspace.add_workspace(str(d))
    assert added is True
    assert path == cfg
    assert cfg.exists()
    # round-trips through config_workspaces
    assert workspace.config_workspaces() == [str(d)]


@pytest.mark.unit
def test_add_workspace_duplicate_by_resolved_path_returns_false(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    d = tmp_path / "proj"
    d.mkdir()
    workspace.add_workspace(str(d))
    # add the same dir via a non-normalized path (trailing-dot component) -> resolves equal
    dup = str(d / ".")
    added, path = workspace.add_workspace(dup)
    assert added is False
    assert path == cfg
    # not appended a second time
    assert workspace.config_workspaces() == [str(d)]


@pytest.mark.unit
def test_remove_workspace_present_returns_true(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    workspace.add_workspace(str(a))
    workspace.add_workspace(str(b))
    assert workspace.remove_workspace(str(a)) is True
    assert workspace.config_workspaces() == [str(b)]


@pytest.mark.unit
def test_remove_workspace_absent_returns_false(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    a = tmp_path / "a"
    a.mkdir()
    workspace.add_workspace(str(a))
    absent = tmp_path / "nope"
    assert workspace.remove_workspace(str(absent)) is False
    # unchanged
    assert workspace.config_workspaces() == [str(a)]


@pytest.mark.unit
def test_remove_workspace_by_resolved_path(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    a = tmp_path / "a"
    a.mkdir()
    workspace.add_workspace(str(a))
    # remove via a non-normalized but resolved-equal path
    assert workspace.remove_workspace(str(a / ".")) is True
    assert workspace.config_workspaces() == []


@pytest.mark.unit
def test_write_config_workspaces_roundtrip_and_escaping(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    # include a path with a double-quote and a backslash to exercise escaping
    weird = str(tmp_path / 'we"ird\\path')
    normal = str(tmp_path / "n")
    written = workspace._write_config_workspaces([normal, weird])
    assert written.exists()
    # round-trips exactly (raw) through config_workspaces
    assert workspace.config_workspaces() == [normal, weird]


@pytest.mark.unit
def test_write_config_creates_parent_dir(monkeypatch, tmp_path):
    nested = tmp_path / "deep" / "nested" / "config.toml"
    monkeypatch.setenv("QUARRY_CONFIG", str(nested))
    workspace._write_config_workspaces([str(tmp_path / "x")])
    assert nested.exists()
    assert nested.parent.is_dir()


# ---------------------------------------------------------------------------
# workspace.py — _split_dirs precedence + build_workspaces + configure
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_split_dirs_explicit_wins(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "fromcfg"
    cfg_dir.mkdir()
    cfg.write_text(f'workspaces = ["{cfg_dir}"]\n', encoding="utf-8")
    e1 = tmp_path / "e1"
    e2 = tmp_path / "e2"
    explicit = os.pathsep.join([str(e1), str(e2)])
    dirs = workspace._split_dirs(explicit)
    assert dirs == [e1.expanduser().resolve(), e2.expanduser().resolve()]


@pytest.mark.unit
def test_split_dirs_explicit_blank_falls_through_to_config(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "fromcfg"
    cfg_dir.mkdir()
    cfg.write_text(f'workspaces = ["{cfg_dir}"]\n', encoding="utf-8")
    # explicit is only separators/whitespace -> no parts -> config used
    dirs = workspace._split_dirs(os.pathsep + "   " + os.pathsep)
    assert dirs == [cfg_dir.resolve()]


@pytest.mark.unit
def test_split_dirs_config_used_when_no_explicit(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "fromcfg"
    cfg_dir.mkdir()
    cfg.write_text(f'workspaces = ["{cfg_dir}"]\n', encoding="utf-8")
    assert workspace._split_dirs(None) == [cfg_dir.resolve()]


@pytest.mark.unit
def test_split_dirs_cwd_fallback(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)  # absent config -> []
    monkeypatch.chdir(tmp_path)
    dirs = workspace._split_dirs(None)
    assert dirs == [Path.cwd()]


@pytest.mark.unit
def test_build_workspaces_primary_paths_and_psql_default(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.delenv("QUARRY_PSQL", raising=False)
    monkeypatch.delenv("QUARRY_CONNECTIONS_FILE", raising=False)
    monkeypatch.delenv("QUARRY_QUERIES_DIR", raising=False)
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    explicit = os.pathsep.join([str(d1), str(d2)])
    wss = workspace.build_workspaces(explicit)
    assert len(wss) == 2
    w0 = wss[0]
    assert isinstance(w0, workspace.Workspace)
    assert w0.home == d1.resolve()
    assert w0.connections_file == d1.resolve() / "connections.toml"
    assert w0.queries_dir == d1.resolve() / "queries"
    assert w0.psql_bin == "psql"
    # second workspace uses its own dir for both
    assert wss[1].connections_file == d2.resolve() / "connections.toml"
    assert wss[1].queries_dir == d2.resolve() / "queries"


@pytest.mark.unit
def test_build_workspaces_env_overrides_only_primary(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    cfile = tmp_path / "custom_conns.toml"
    qdir = tmp_path / "custom_queries"
    monkeypatch.setenv("QUARRY_CONNECTIONS_FILE", str(cfile))
    monkeypatch.setenv("QUARRY_QUERIES_DIR", str(qdir))
    monkeypatch.setenv("QUARRY_PSQL", "/opt/custom/psql")
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    explicit = os.pathsep.join([str(d1), str(d2)])
    wss = workspace.build_workspaces(explicit)
    # primary (i==0) picks up the overrides
    assert wss[0].connections_file == cfile.expanduser()
    assert wss[0].queries_dir == qdir.expanduser()
    assert wss[0].psql_bin == "/opt/custom/psql"
    # secondary (i==1) does NOT — falls back to its own dir
    assert wss[1].connections_file == d2.resolve() / "connections.toml"
    assert wss[1].queries_dir == d2.resolve() / "queries"
    # but psql_bin is shared (from env)
    assert wss[1].psql_bin == "/opt/custom/psql"


@pytest.mark.unit
def test_configure_workspace_rebinds_globals(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    d = tmp_path / "cfgd"
    d.mkdir()
    try:
        ws = workspace.configure_workspace(str(d))
        assert ws is workspace.WS
        assert workspace.WS.home == d.resolve()
        assert workspace.WS_LIST[0] is workspace.WS
        assert len(workspace.WS_LIST) == 1
    finally:
        # reset global state so we don't leak to other tests
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_workspace_dataclass_fields():
    w = workspace.Workspace(
        home=Path("/h"),
        connections_file=Path("/h/connections.toml"),
        queries_dir=Path("/h/queries"),
        psql_bin="psql",
    )
    assert w.home == Path("/h")
    assert w.connections_file == Path("/h/connections.toml")
    assert w.queries_dir == Path("/h/queries")
    assert w.psql_bin == "psql"


# ---------------------------------------------------------------------------
# tunnel.py — pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "url,engine,expected",
    [
        ("postgresql://user@dbhost:6000/mydb", "postgres", ("dbhost", 6000)),
        ("postgresql://user@dbhost/mydb", "postgres", ("dbhost", 5432)),
        ("redis://cache:6380/0", "redis", ("cache", 6380)),
        ("redis://cache/0", "redis", ("cache", 6379)),
        ("mysql://u:p@sqlhost:3307/db", "mysql", ("sqlhost", 3307)),
        ("mysql://u:p@sqlhost/db", "mysql", ("sqlhost", 3306)),
        # scheme-less host:port/db -> urlparse via //-prefix
        ("host:5432/db", "postgres", ("host", 5432)),
        ("host/db", "postgres", ("host", 5432)),
    ],
)
def test_db_host_port(url, engine, expected):
    assert tunnel._db_host_port(url, engine) == expected


@pytest.mark.unit
def test_db_host_port_unknown_engine_defaults_5432():
    # engine not in DEFAULT_DB_PORT and no explicit port -> 5432
    assert tunnel._db_host_port("neptune://gw/graph", "neptune") == ("gw", 5432)


@pytest.mark.unit
def test_db_host_port_empty_host_falls_back_to_localhost():
    # no hostname parsed -> 127.0.0.1
    host, port = tunnel._db_host_port("postgresql:///db", "postgres")
    assert host == "127.0.0.1"
    assert port == 5432


@pytest.mark.unit
def test_rewrite_url_hostport_user_and_pass():
    out = tunnel._rewrite_url_hostport(
        "postgresql://alice:secret@remote:5432/db", "127.0.0.1", 15000
    )
    assert out == "postgresql://alice:secret@127.0.0.1:15000/db"


@pytest.mark.unit
def test_rewrite_url_hostport_password_only_userinfo():
    # redis://:pw@host  -> username empty, password present
    out = tunnel._rewrite_url_hostport(
        "redis://:mypw@cache:6379/0", "127.0.0.1", 16000
    )
    assert out == "redis://:mypw@127.0.0.1:16000/0"


@pytest.mark.unit
def test_rewrite_url_hostport_user_only_userinfo():
    out = tunnel._rewrite_url_hostport(
        "postgresql://bob@remote:5432/db", "127.0.0.1", 17000
    )
    assert out == "postgresql://bob@127.0.0.1:17000/db"


@pytest.mark.unit
def test_rewrite_url_hostport_no_userinfo():
    out = tunnel._rewrite_url_hostport(
        "postgresql://remote:5432/db", "127.0.0.1", 18000
    )
    assert out == "postgresql://127.0.0.1:18000/db"


@pytest.mark.unit
def test_rewrite_url_hostport_preserves_path_query():
    out = tunnel._rewrite_url_hostport(
        "postgresql://u:p@remote:5432/db?sslmode=require", "127.0.0.1", 19000
    )
    assert out == "postgresql://u:p@127.0.0.1:19000/db?sslmode=require"


@pytest.mark.unit
def test_free_port_returns_int_in_range():
    p = tunnel._free_port()
    assert isinstance(p, int)
    assert 1 <= p <= 65535


# ---------------------------------------------------------------------------
# tunnel.py — _wait_port branches (mock socket / proc)
# ---------------------------------------------------------------------------

class _FakeProc:
    """Stand-in for subprocess.Popen. poll() returns _poll_value."""

    def __init__(self, poll_value=None):
        self._poll_value = poll_value
        self.terminated = False
        self.waited = False

    def poll(self):
        return self._poll_value

    def die(self, code=1):
        self._poll_value = code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        return self._poll_value

    def communicate(self, timeout=None):
        return (b"", b"boom: connection refused")


@pytest.mark.unit
def test_wait_port_ready(monkeypatch):
    import contextlib as _ctx

    @_ctx.contextmanager
    def fake_create_connection(addr, timeout=None):
        yield object()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    proc = _FakeProc(poll_value=None)  # alive
    assert tunnel._wait_port("127.0.0.1", 12345, proc, timeout=1.0) is True


@pytest.mark.unit
def test_wait_port_proc_exits_early(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("create_connection should not be called when proc is dead")

    monkeypatch.setattr(socket, "create_connection", boom)
    proc = _FakeProc(poll_value=1)  # already dead
    assert tunnel._wait_port("127.0.0.1", 12345, proc, timeout=1.0) is False


@pytest.mark.unit
def test_wait_port_timeout(monkeypatch):
    # connection always refused + proc stays alive -> loop until deadline -> False
    def refuse(*a, **k):
        raise OSError("refused")

    slept = []
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(tunnel.time, "sleep", lambda s: slept.append(s))
    proc = _FakeProc(poll_value=None)
    # tiny timeout so the monotonic deadline passes quickly
    assert tunnel._wait_port("127.0.0.1", 12345, proc, timeout=0.01) is False


# ---------------------------------------------------------------------------
# tunnel.py — open_tunnel / pool / close_all (mock Popen + _wait_port)
# ---------------------------------------------------------------------------

class _Conn:
    def __init__(self, url, ssh_host=None, ssh_user=None, ssh_key=None, ssh_port=None):
        self.url = url
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key
        self.ssh_port = ssh_port


@pytest.fixture(autouse=True)
def _clear_pool():
    """Never leak pooled fake procs across tests / to real close_all at exit."""
    tunnel._POOL.clear()
    yield
    tunnel._POOL.clear()


@pytest.mark.unit
def test_open_tunnel_no_ssh_host_yields_url_unchanged():
    conn = _Conn("postgresql://u@dbhost:5432/db")
    with tunnel.open_tunnel(conn, "postgres") as url:
        assert url == conn.url
    # nothing pooled
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_open_tunnel_with_ssh_rewrites_and_pools(monkeypatch):
    fake = _FakeProc(poll_value=None)  # alive
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54321)

    conn = _Conn(
        "postgresql://alice:pw@remote-db:5432/app",
        ssh_host="bastion.example.com",
        ssh_user="deploy",
    )
    with tunnel.open_tunnel(conn, "postgres") as url:
        assert url == "postgresql://alice:pw@127.0.0.1:54321/app"

    # exactly one tunnel spawned + pooled
    assert len(popen_calls) == 1
    assert len(tunnel._POOL) == 1
    # command targets the right bastion + forward
    cmd = popen_calls[0]
    assert cmd[0] == "ssh"
    assert "-L" in cmd
    li = cmd.index("-L")
    assert cmd[li + 1] == "127.0.0.1:54321:remote-db:5432"
    assert "deploy@bastion.example.com" in cmd


@pytest.mark.unit
def test_open_tunnel_pool_reuse_second_call(monkeypatch):
    fake = _FakeProc(poll_value=None)
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 44444)

    conn = _Conn(
        "postgresql://u@remote-db:5432/app",
        ssh_host="bastion",
        ssh_user="root",
    )
    with tunnel.open_tunnel(conn, "postgres") as url1:
        pass
    with tunnel.open_tunnel(conn, "postgres") as url2:
        pass
    assert url1 == url2
    # only spawned once — pool reuse on the second call
    assert len(popen_calls) == 1
    assert len(tunnel._POOL) == 1


@pytest.mark.unit
def test_open_tunnel_dead_pooled_proc_respawns(monkeypatch):
    procs = [_FakeProc(poll_value=1), _FakeProc(poll_value=None)]  # 1st dead, 2nd alive
    made = []

    def fake_popen(cmd, **kwargs):
        p = procs[len(made)]
        made.append(p)
        return p

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 33333)

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    # First call: pool a proc that is already 'dead' (poll->1)
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    # Second call: pooled proc not alive -> discarded + a fresh one spawned
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert len(made) == 2


@pytest.mark.unit
def test_open_tunnel_failed_tunnel_raises(monkeypatch):
    from quarry.core import QuarryError

    dead = _FakeProc(poll_value=1)  # ssh dies immediately

    def fake_popen(cmd, **kwargs):
        return dead

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 22222)
    # do not patch _wait_port -> real one runs; proc.poll()->1 so it returns False fast

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    with pytest.raises(QuarryError) as ei:
        with tunnel.open_tunnel(conn, "postgres"):
            pass
    assert "ssh tunnel to host failed" in str(ei.value)
    assert "connection refused" in str(ei.value)
    assert dead.terminated is True
    # failed tunnel is not pooled
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_make_tunnel_missing_ssh_key_raises(monkeypatch, tmp_path):
    from quarry.core import EXIT_CONNECTION_ERROR, QuarryError

    # Popen must never be reached — the missing key errors first.
    def boom_popen(*a, **k):
        raise AssertionError("Popen must not run when the ssh key is missing")

    monkeypatch.setattr(tunnel.subprocess, "Popen", boom_popen)
    missing_key = tmp_path / "no_such_key"
    conn = _Conn(
        "postgresql://u@remote:5432/app",
        ssh_host="host",
        ssh_key=str(missing_key),
    )
    with pytest.raises(QuarryError) as ei:
        tunnel._make_tunnel(conn, "remote", 5432)
    assert "ssh key not found" in str(ei.value)
    assert ei.value.exit_code == EXIT_CONNECTION_ERROR


@pytest.mark.unit
def test_make_tunnel_uses_key_and_port_in_cmd(monkeypatch, tmp_path):
    key = tmp_path / "id_bastion"
    key.write_text("KEY", encoding="utf-8")
    fake = _FakeProc(poll_value=None)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 45678)

    conn = _Conn(
        "postgresql://u@remote:5432/app",
        ssh_host="bastion",
        ssh_user="ops",
        ssh_key=str(key),
        ssh_port=2222,
    )
    t = tunnel._make_tunnel(conn, "remote", 5432)
    assert t.local_port == 45678
    assert t.alive() is True
    cmd = captured["cmd"]
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == str(key)
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "2222"
    assert "ops@bastion" in cmd


@pytest.mark.unit
def test_tunnel_alive_reflects_poll():
    alive = tunnel._Tunnel(_FakeProc(poll_value=None), 5000)
    dead = tunnel._Tunnel(_FakeProc(poll_value=0), 5001)
    assert alive.alive() is True
    assert dead.alive() is False


@pytest.mark.unit
def test_close_all_terminates_pooled_and_clears(monkeypatch):
    p1 = _FakeProc(poll_value=None)
    p2 = _FakeProc(poll_value=None)
    tunnel._POOL[("k1",)] = tunnel._Tunnel(p1, 5000)
    tunnel._POOL[("k2",)] = tunnel._Tunnel(p2, 5001)
    tunnel.close_all()
    assert p1.terminated is True
    assert p2.terminated is True
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_close_all_swallows_terminate_errors():
    class _BadProc(_FakeProc):
        def terminate(self):
            raise RuntimeError("already gone")

    tunnel._POOL[("k",)] = tunnel._Tunnel(_BadProc(), 5000)
    # must not raise
    tunnel.close_all()
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_make_tunnel_wait_fail_surfaces_stderr(monkeypatch):
    """_wait_port False -> terminate + communicate stderr surfaced in the error."""
    from quarry.core import QuarryError

    fake = _FakeProc(poll_value=None)  # 'alive' at spawn but port never opens

    def fake_popen(cmd, **kwargs):
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: False)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 11111)

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    with pytest.raises(QuarryError) as ei:
        tunnel._make_tunnel(conn, "remote", 5432)
    # stderr from communicate() bubbles up
    assert "connection refused" in str(ei.value)
    assert fake.terminated is True


@pytest.mark.unit
def test_make_tunnel_wait_fail_cleanup_swallows_exception(monkeypatch):
    """When _wait_port fails and terminate()/communicate() themselves raise, the
    cleanup except-block swallows it and we fall back to the generic detail."""
    from quarry.core import QuarryError

    class _BadCleanupProc(_FakeProc):
        def terminate(self):
            raise RuntimeError("cannot terminate")

        def communicate(self, timeout=None):
            raise RuntimeError("cannot communicate")

    bad = _BadCleanupProc(poll_value=None)
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: bad)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: False)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 10101)

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    with pytest.raises(QuarryError) as ei:
        tunnel._make_tunnel(conn, "remote", 5432)
    # no stderr captured -> generic fallback detail
    assert "port not ready / timeout" in str(ei.value)
