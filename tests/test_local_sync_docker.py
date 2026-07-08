"""Real-docker integration tests for `qy local sync`.

Uses a throwaway Postgres container as the sync target and the suite's
`quarry_test` database (when reachable) as the source. Skips when docker or
the test DB is unavailable — same policy as test_local_docker.py.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from conftest import TEST_DB_URL, requires_db, requires_docker
from quarry import core, local, local_sync, workspace

PG_IMAGE = "postgres:16-alpine"
REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_dump() -> str | None:
    import shutil
    for cand in ("pg_dump", "/opt/homebrew/opt/postgresql@13/bin/pg_dump"):
        if shutil.which(cand):
            return cand
    return None


requires_pg_dump = pytest.mark.skipif(not _pg_dump(), reason="pg_dump not in PATH")


@pytest.fixture()
def pg_spec():
    uniq = uuid.uuid4().hex[:8]
    spec = local.EngineSpec(
        engine="postgres",
        container=f"quarry-test-sync-pg-{uniq}",
        volume=f"quarry-test-sync-pgvol-{uniq}",
        port=_free_port(),
        internal_port=5432,
        default_image=PG_IMAGE,
    )
    try:
        yield spec
    finally:
        local._run_docker(["rm", "-f", spec.container], timeout=60)
        local._run_docker(["volume", "rm", spec.volume], timeout=30)


@pytest.fixture()
def sync_ws(tmp_path: Path, pg_spec):
    """Workspace: quarry_test as dev source, docker container as local target."""
    logical = "syncshop"
    local_url = pg_spec.url(logical)
    (tmp_path / "connections.toml").write_text(
        f'[{logical}_dev]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\n'
        f'env = "dev"\ndb = "{logical}"\n\n'
        f'[{logical}_local]\nurl = "{local_url}"\nengine = "postgres"\n'
        f'env = "local"\ndb = "{logical}"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    local.start_container(pg_spec, image=PG_IMAGE)
    assert local.wait_pg_ready(pg_spec, timeout=60)
    local.ensure_pg_database(pg_spec, logical)
    yield tmp_path, logical, local_url
    workspace.configure_workspace(None)


def _psql(url: str, sql: str) -> tuple[int, str, str]:
    return core.run_psql_capture(url, sql, timeout=30)


def _hold_connection(url: str) -> subprocess.Popen:
    """Open a long-lived session on the target DB (outside sync's control)."""
    psql = core.resolve_psql()
    return subprocess.Popen(
        [psql, url, "--no-psqlrc", "-c", "BEGIN; SELECT pg_sleep(600);"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_empty_local_matches_source(sync_ws):
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_clears_stale_objects_on_resync(sync_ws):
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    rc, _, err = _psql(
        local_url,
        "CREATE TABLE leftover_garbage(id int); "
        "INSERT INTO customers(name, email) VALUES ('stale', 'stale@ex.com');",
    )
    assert rc == 0, err

    local_sync.sync_schema(logical, from_env="dev")
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)

    rc, out, err = _psql(
        local_url,
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='leftover_garbage'",
    )
    assert rc == 0 and out.strip() == "0", err

    rc, out, err = _psql(local_url, "SELECT COUNT(*) FROM customers")
    assert rc == 0 and out.strip() == "0", err


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_terminates_concurrent_holder(sync_ws):
    _ws, logical, local_url = sync_ws
    holder = _hold_connection(local_url)
    try:
        local_sync.sync_schema(logical, from_env="dev")
        local_sync.assert_schemas_match(TEST_DB_URL, local_url)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # holder should have been terminated (not still sleeping)
    assert holder.poll() is not None


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_cli_local_sync_subprocess(sync_ws):
    ws, logical, _local_url = sync_ws
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(ws),
         "local", "sync", logical, "--from", "dev"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "synced schema" in proc.stdout


@pytest.mark.integration
@requires_db
def test_cli_rejects_non_local_target(tmp_path):
    """Sync must refuse when the resolved target is not env=local."""
    (tmp_path / "connections.toml").write_text(
        f'[only_dev]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "dev"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(tmp_path),
         "local", "sync", "only_dev"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == core.EXIT_SYNC_DENIED
    assert "sync refused" in proc.stderr
