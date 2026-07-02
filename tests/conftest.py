"""Test fixtures — a throwaway workspace pointing at a local Postgres.

Requires a reachable Postgres with a `quarry_test` database seeded with
`customers` and `orders` (see tests/seed.sql). Set QUARRY_TEST_DB_URL to
override the connection URL. DB-backed tests skip if the DB is unreachable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quarry import workspace  # noqa: E402

TEST_DB_URL = os.environ.get(
    "QUARRY_TEST_DB_URL", "postgresql://localhost:5432/quarry_test"
)


def _psql() -> str | None:
    for cand in ("psql", "/opt/homebrew/opt/postgresql@13/bin/psql"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    return None


def _db_reachable() -> bool:
    psql = _psql()
    if not psql:
        return False
    try:
        proc = subprocess.run(
            [psql, TEST_DB_URL, "-tAc", "SELECT 1"],
            capture_output=True, text=True, timeout=8,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "1"
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_reachable(), reason="quarry_test Postgres not reachable")


@pytest.fixture()
def ws(tmp_path: Path):
    """A temp workspace with one connection (testpg) + an empty queries dir."""
    if _psql() and _psql() != "psql":
        os.environ["QUARRY_PSQL"] = _psql()  # point at brew psql if needed
    conn_file = tmp_path / "connections.toml"
    conn_file.write_text(
        f'[testpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path
    workspace.configure_workspace(None)
