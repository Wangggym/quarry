"""CLI end-to-end tests via subprocess against the local Postgres workspace."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TEST_DB_URL, requires_db

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def run_qy(workspace_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.pop("QUARRY_WORKSPACE", None)
    # explicit --workspace outranks any real config.toml / env on the dev machine
    return subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(workspace_dir), *args],
        capture_output=True, text=True, env=env, timeout=20,
    )


@pytest.fixture()
def wsdir(tmp_path: Path) -> Path:
    (tmp_path / "connections.toml").write_text(
        f'[testpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n', encoding="utf-8")
    (tmp_path / "queries").mkdir()
    return tmp_path


def test_connections_list_json(wsdir):
    p = run_qy(wsdir, "connections", "--format", "json")
    assert p.returncode == 0, p.stderr
    tree = json.loads(p.stdout)
    item = tree[0]["items"][0]
    assert item["db"] == "testpg" and item["engine"] == "postgres"
    assert item["envs"][0]["key"] == "testpg"


@requires_db
def test_exec_json(wsdir):
    p = run_qy(wsdir, "exec", "testpg", "--sql", "select count(*) n from customers", "--format", "json")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)[0]["n"] == 3


@requires_db
def test_exec_blocks_write_by_default(wsdir):
    p = run_qy(wsdir, "exec", "testpg", "--sql", "delete from orders")
    assert p.returncode == 8  # EXIT_SAFETY_BLOCKED
    assert "read-only" in p.stderr


@requires_db
def test_exec_table_format(wsdir):
    p = run_qy(wsdir, "exec", "testpg", "--sql", "select name from customers order by id", "--format", "table")
    assert p.returncode == 0, p.stderr
    assert "Alice" in p.stdout and "name" in p.stdout


@requires_db
def test_save_run_roundtrip(wsdir):
    save = run_qy(wsdir, "save", "active_customers", "--db", "testpg",
                  "--desc", "all customers", "--sql", "select id, name from customers order by id")
    assert save.returncode == 0, save.stderr
    assert (wsdir / "queries" / "testpg" / "active_customers.sql").exists()
    run = run_qy(wsdir, "run", "active_customers", "--format", "json")
    assert run.returncode == 0, run.stderr
    rows = json.loads(run.stdout)
    assert len(rows) == 3 and rows[0]["name"] == "Alice"

    lst = run_qy(wsdir, "list", "--format", "json")
    assert any(q["name"] == "active_customers" for q in json.loads(lst.stdout))
