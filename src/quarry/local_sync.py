"""Schema sync from a remote env into a local Postgres — `qy local sync`.

Copies structure with `pg_dump --schema-only` (no migration framework). The target
must resolve to `env=local`; there is no override for that safety gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import core, local, tunnel, workspace
from .core import (
    EXIT_CONNECTION_ERROR,
    EXIT_SAFETY_BLOCKED,
    EXIT_SQL_ERROR,
    EXIT_SYNC_DENIED,
    EXIT_USAGE,
    QuarryError,
    connection_engine,
    run_psql_capture,
)

_PG_VERSION_RE = re.compile(r"(?:PostgreSQL\s+)?(\d+)(?:\.\d+)?")


def resolve_pg_dump() -> str:
    """Locate the `pg_dump` binary (mirrors resolve_psql fallbacks)."""
    psql_bin = workspace.WS.psql_bin
    psql_path = Path(psql_bin)
    if psql_path.name == "psql":
        dump_bin = psql_path.with_name("pg_dump")
        if dump_bin.exists():
            return str(dump_bin)
    if shutil.which("pg_dump"):
        return "pg_dump"
    homebrew = "/opt/homebrew/opt/postgresql@13/bin/pg_dump"
    if Path(homebrew).exists():
        return homebrew
    raise QuarryError(
        "pg_dump not found in PATH (install postgresql client tools or set QUARRY_PSQL's bin dir)",
        exit_code=EXIT_CONNECTION_ERROR,
    )


def parse_pg_major(version_text: str) -> int:
    m = _PG_VERSION_RE.search(version_text.strip())
    if not m:
        raise QuarryError(
            f"cannot parse PostgreSQL version from: {version_text!r}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return int(m.group(1))


def pg_dump_major_version(dump_bin: str | None = None) -> int:
    bin_path = dump_bin or resolve_pg_dump()
    try:
        proc = subprocess.run(
            [bin_path, "--version"], capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise QuarryError("pg_dump --version timed out", exit_code=EXIT_CONNECTION_ERROR)
    if proc.returncode != 0:
        raise QuarryError(
            f"pg_dump --version failed: {proc.stderr.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return parse_pg_major(proc.stdout or proc.stderr)


def server_pg_major_version(url: str) -> int:
    rc, out, err = run_psql_capture(
        url, "SHOW server_version", timeout=15,
    )
    if rc != 0:
        raise QuarryError(
            f"failed to read server version: {err.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return parse_pg_major(out)


def assert_pg_dump_compatible(
    server_major: int, *, dump_bin: str | None = None,
) -> None:
    client_major = pg_dump_major_version(dump_bin)
    if client_major < server_major:
        raise QuarryError(
            f"pg_dump client is PostgreSQL {client_major} but source server is "
            f"{server_major} — install a pg_dump at least as new as the server "
            f"(e.g. `brew install postgresql@{server_major}`)",
            exit_code=EXIT_CONNECTION_ERROR,
        )


def _require_local_target(conn: core.Connection) -> None:
    if (conn.env or "").lower() != local.LOCAL_ENV:
        raise QuarryError(
            f"sync refused: target connection [{conn.key}] has env={conn.env!r}, "
            f"not '{local.LOCAL_ENV}' — this command only runs against local databases",
            exit_code=EXIT_SYNC_DENIED,
        )


def _require_postgres(conn: core.Connection, role: str) -> None:
    eng = connection_engine(conn)
    if eng != "postgres":
        raise QuarryError(
            f"sync only supports postgres (the {role} connection [{conn.key}] is {eng})",
            exit_code=EXIT_USAGE,
        )


def run_pg_dump_schema(url: str, *, dump_bin: str | None = None) -> str:
    bin_path = dump_bin or resolve_pg_dump()
    cmd = [bin_path, "--schema-only", "--no-owner", "--no-privileges", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise QuarryError("pg_dump timed out", exit_code=EXIT_CONNECTION_ERROR)
    if proc.returncode != 0:
        raise QuarryError(
            f"pg_dump failed: {proc.stderr.strip() or proc.stdout.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )
    return proc.stdout


def terminate_other_connections(url: str) -> None:
    sql = """
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid();
"""
    rc, _, err = run_psql_capture(url, sql, timeout=30)
    if rc != 0:
        raise QuarryError(
            f"failed to terminate other connections: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


def current_database_name(url: str) -> str:
    rc, out, err = run_psql_capture(url, "SELECT current_database()", timeout=15)
    if rc != 0:
        raise QuarryError(
            f"failed to read database name: {err.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    name = out.strip()
    if not name:
        raise QuarryError(
            "failed to read database name: empty result",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return name


def _db_name_pattern(db: str) -> str:
    """Match a database identifier as pg_dump may quote it."""
    return rf'(?:"{re.escape(db)}"|{re.escape(db)})'


def sanitize_schema_dump(dump: str, *, source_db: str, target_db: str) -> str:
    """Rewrite source-database references and drop connect/create-database lines."""
    if source_db != target_db:
        ref = _db_name_pattern(source_db)
        dump = re.sub(
            rf"COMMENT ON DATABASE {ref}",
            f'COMMENT ON DATABASE "{target_db}"',
            dump,
            flags=re.IGNORECASE,
        )
        dump = re.sub(
            rf"ALTER DATABASE {ref}",
            f'ALTER DATABASE "{target_db}"',
            dump,
            flags=re.IGNORECASE,
        )
    kept: list[str] = []
    for line in dump.splitlines():
        stripped = line.strip()
        if stripped.startswith("\\connect"):
            continue
        if re.match(r"CREATE\s+DATABASE\b", stripped, re.IGNORECASE):
            continue
        kept.append(line)
    out = "\n".join(kept)
    if dump.endswith("\n"):
        out += "\n"
    return out


_RESET_TARGET_SQL = """
DO $quarry$
DECLARE r record;
BEGIN
  FOR r IN SELECT subname FROM pg_subscription LOOP
    EXECUTE format('DROP SUBSCRIPTION %I', r.subname);
  END LOOP;
  FOR r IN SELECT pubname FROM pg_publication LOOP
    EXECUTE format('DROP PUBLICATION %I', r.pubname);
  END LOOP;
  FOR r IN SELECT evtname FROM pg_event_trigger LOOP
    EXECUTE format('DROP EVENT TRIGGER %I', r.evtname);
  END LOOP;
  FOR r IN SELECT srvname FROM pg_foreign_server LOOP
    EXECUTE format('DROP SERVER %I CASCADE', r.srvname);
  END LOOP;
  FOR r IN SELECT fdwname FROM pg_foreign_data_wrapper LOOP
    EXECUTE format('DROP FOREIGN DATA WRAPPER %I CASCADE', r.fdwname);
  END LOOP;
  FOR r IN SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' LOOP
    EXECUTE format('DROP EXTENSION IF EXISTS %I CASCADE', r.extname);
  END LOOP;
  EXECUTE format('COMMENT ON DATABASE %I IS NULL', current_database());
END
$quarry$;
DO $quarry$
DECLARE sch text;
BEGIN
  FOR sch IN
    SELECT nspname FROM pg_namespace
    WHERE nspname !~ '^pg_'
      AND nspname <> 'information_schema'
  LOOP
    EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', sch);
  END LOOP;
END
$quarry$;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO public;
"""


def reset_user_schemas(url: str) -> None:
    """Wipe database-level objects and user schemas so pg_dump can reapply idempotently."""
    rc, _, err = run_psql_capture(url, _RESET_TARGET_SQL, timeout=60)
    if rc != 0:
        raise QuarryError(
            f"failed to reset target database: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


def apply_schema_dump(url: str, dump: str) -> None:
    rc, _, err = run_psql_capture(url, dump, timeout=120)
    if rc != 0:
        raise QuarryError(
            f"failed to apply schema dump: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


SCHEMA_COLUMNS_SQL = """
SELECT table_schema, table_name, column_name,
       data_type, character_maximum_length, numeric_precision,
       numeric_scale, is_nullable, udt_name
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND table_name NOT LIKE 'pg_%'
ORDER BY table_schema, table_name, ordinal_position;
"""


def fetch_schema_columns(url: str) -> list[tuple[str, ...]]:
    rc, out, err = run_psql_capture(url, SCHEMA_COLUMNS_SQL, timeout=30)
    if rc != 0:
        raise QuarryError(
            f"failed to read information_schema.columns: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )
    rows: list[tuple[str, ...]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        rows.append(tuple(part.strip() for part in line.split("|")))
    return rows


def assert_schemas_match(source_url: str, target_url: str) -> None:
    """Programmatic schema equality check via information_schema.columns."""
    src = fetch_schema_columns(source_url)
    tgt = fetch_schema_columns(target_url)
    if src != tgt:
        src_tables = sorted({r[1] for r in src})
        tgt_tables = sorted({r[1] for r in tgt})
        raise AssertionError(
            f"schema mismatch: source tables {src_tables!r} vs target {tgt_tables!r} "
            f"({len(src)} source columns vs {len(tgt)} target columns)"
        )


def sync_schema(
    key: str,
    *,
    from_env: str = "dev",
    dump_bin: str | None = None,
) -> None:
    """Copy schema from `key`@from_env into `key`@local. Raises QuarryError on failure."""
    source = core.resolve_connection(key, from_env)
    target = core.resolve_connection(key, local.LOCAL_ENV)
    _require_local_target(target)
    _require_postgres(source, "source")
    _require_postgres(target, "target")

    with tunnel.open_tunnel(source, "postgres") as source_url, \
         tunnel.open_tunnel(target, "postgres") as target_url:
        server_major = server_pg_major_version(source_url)
        assert_pg_dump_compatible(server_major, dump_bin=dump_bin)
        source_db = current_database_name(source_url)
        target_db = current_database_name(target_url)
        dump = run_pg_dump_schema(source_url, dump_bin=dump_bin)
        dump = sanitize_schema_dump(dump, source_db=source_db, target_db=target_db)
        terminate_other_connections(target_url)
        reset_user_schemas(target_url)
        apply_schema_dump(target_url, dump)
