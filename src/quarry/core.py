"""Quarry core — the engine.

Lifted from the `dbq` CLI, generalized to be workspace-driven and importable
as a library (the CLI, GUI, and skill are all thin faces over this module).

Engines:
    postgres  - shells out to the `psql` binary
    mysql     - via pymysql (optional dependency)
    neptune   - openCypher over HTTP (urllib)

Paths (connections file, queries dir, psql binary) come from the current
Workspace (see workspace.py); reference `workspace.WS` at call time so that
--workspace reconfiguration is honored.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from . import redis_engine, tunnel, workspace

# Exit codes (stable contract for callers)
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONNECTION_ERROR = 2
EXIT_SQL_ERROR = 3
EXIT_NO_DATA = 4
EXIT_STRICT_DRIFT = 5
EXIT_FINGERPRINT_STALE = 6
EXIT_FINGERPRINT_MISSING = 7
EXIT_SAFETY_BLOCKED = 8   # write/DDL blocked without --write
EXIT_SYNC_DENIED = 9      # `qy local sync` refused: target is not env=local on a loopback host

NEPTUNE_TIMEOUT_SEC = int(os.environ.get("QUARRY_NEPTUNE_TIMEOUT", "60"))
NEPTUNE_INSECURE = os.environ.get("QUARRY_NEPTUNE_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}

# Default safety cap on rows returned when the SQL has no explicit LIMIT.
DEFAULT_MAX_ROWS = int(os.environ.get("QUARRY_MAX_ROWS", "500"))


class QuarryError(Exception):
    """Engine-level error carrying a stable exit code (raised by the library API)."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def err(msg: str, *, exit_code: int | None = None) -> None:
    """With exit_code: raise QuarryError (so library callers like the GUI get a
    catchable error instead of a process-killing SystemExit; the CLI converts it
    to an exit code in main()). Without exit_code: print a non-fatal warning."""
    if exit_code is not None:
        raise QuarryError(msg, exit_code)
    print(f"quarry: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_psql() -> str:
    psql_bin = workspace.WS.psql_bin
    if shutil.which(psql_bin):
        return psql_bin
    homebrew = "/opt/homebrew/opt/postgresql@13/bin/psql"
    if Path(homebrew).exists():
        return homebrew
    err("psql not found in PATH (set QUARRY_PSQL or install postgresql)", exit_code=EXIT_CONNECTION_ERROR)
    return ""  # unreachable


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@dataclass
class Connection:
    key: str
    url: str
    region: str | None = None
    env: str | None = None
    notes: str | None = None
    engine: str = "postgres"
    # Optional SSH bastion (set -> queries are tunneled, see tunnel.py)
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key: str | None = None
    ssh_port: int | None = None
    # Organization: `group` = sidebar folder (project); `db` = logical database
    # identity. Connections sharing `db` (same schema, different env) form an
    # env-set and share saved queries.
    group: str | None = None
    db: str | None = None
    source: str | None = None   # workspace home this connection was loaded from

    @property
    def logical_db(self) -> str:
        return self.db or self.key


# Default env picked for an env-set when none is specified (safest = dev).
DEFAULT_ENV = os.environ.get("QUARRY_DEFAULT_ENV", "dev")


def load_connections() -> dict[str, Connection]:
    wss = workspace.WS_LIST
    if not any(w.connections_file.exists() for w in wss):
        err(f"connections file not found: {wss[0].connections_file}", exit_code=EXIT_USAGE)
    out: dict[str, Connection] = {}
    for w in wss:
        conn_file = w.connections_file
        if not conn_file.exists():
            continue
        with conn_file.open("rb") as f:
            raw = tomllib.load(f)
        for key, val in raw.items():
            if key in out:  # earlier workspace wins on conflict
                continue
            if not isinstance(val, dict) or "url" not in val:
                err(f"connection [{key}] is missing required 'url'", exit_code=EXIT_USAGE)
            ssh_port = val.get("ssh_port")
            out[key] = Connection(
            key=key,
            url=val["url"],
            region=val.get("region"),
            env=val.get("env"),
            notes=val.get("notes"),
            engine=infer_engine(val["url"], val.get("engine")),
            ssh_host=val.get("ssh_host"),
            ssh_user=val.get("ssh_user"),
            ssh_key=val.get("ssh_key"),
            ssh_port=int(ssh_port) if ssh_port else None,
            group=val.get("group"),
            db=val.get("db"),
            source=str(w.home),
        )
    return out


def resolve_connection(name: str, env: str | None = None) -> Connection:
    """Resolve a target to a Connection.

    `name` may be a connection key OR a logical db (env-set) name.
      - direct key, no --env  -> that connection (backward compatible)
      - logical db / --env     -> the env-set member for that env (default: dev)
    """
    conns = load_connections()
    if env is None and name in conns:
        return conns[name]

    # `name` may be a logical db OR a connection key — search the env-set by
    # logical db either way (so `@db: shop_dev` + --env jp still
    # resolves to the shop env-set's jp member, keeping legacy query
    # files working).
    logical = conns[name].logical_db if name in conns else name
    members = {c.env or "": c for c in conns.values() if c.logical_db == logical}
    if members:
        target = env or DEFAULT_ENV
        if target in members:
            return members[target]
        if env is None and len(members) == 1:
            return next(iter(members.values()))
        if env is None:  # multi-env set, no dev -> pick a stable first
            return members[sorted(members)[0]]
        if name in conns:  # env given but not in set -> fall back to the key
            return conns[name]
        avail = ", ".join(sorted(e for e in members if e)) or "<none>"
        err(f"env '{env}' not found for '{name}'. Available: {avail}", exit_code=EXIT_USAGE)

    if name in conns:
        return conns[name]
    available = ", ".join(sorted(conns.keys())) or "<none>"
    err(f"unknown db '{name}'. Available: {available}", exit_code=EXIT_USAGE)
    raise SystemExit(EXIT_USAGE)  # unreachable


def group_connections() -> list[dict[str, Any]]:
    """Structured view for CLI/GUI: [{group, items: [{db, is_env_set, envs:[...]}]}]."""
    conns = list(load_connections().values())
    groups: dict[str, dict[str, list[Connection]]] = {}
    gsrc: dict[str, str | None] = {}
    order: list[str] = []
    for c in conns:
        g = c.group or ""
        if g not in groups:
            groups[g] = {}
            gsrc[g] = c.source
            order.append(g)
        groups[g].setdefault(c.logical_db, []).append(c)

    out: list[dict[str, Any]] = []
    for g in order:
        items = []
        for ldb, members in groups[g].items():
            items.append({
                "db": ldb,
                "is_env_set": len(members) > 1 or bool(members[0].env),
                "engine": connection_engine(members[0]),
                "envs": [
                    {"env": m.env, "key": m.key, "engine": connection_engine(m),
                     "region": m.region, "ssh": bool(m.ssh_host)}
                    for m in members
                ],
            })
        out.append({"group": g or None, "ws": gsrc.get(g), "items": items})
    return out


def infer_engine(url: str, explicit: str | None = None) -> str:
    if explicit:
        engine = explicit.strip().lower()
        if engine not in {"postgres", "mysql", "neptune", "redis"}:
            err(f"unsupported engine '{explicit}' (expected postgres|mysql|neptune|redis)", exit_code=EXIT_USAGE)
        return engine
    lower = url.lower()
    if lower.startswith("mysql://") or lower.startswith("mysql+"):
        return "mysql"
    if lower.startswith("redis://") or lower.startswith("rediss://"):
        return "redis"
    if "neptune.amazonaws.com" in lower:
        return "neptune"
    return "postgres"


def connection_engine(conn: Connection) -> str:
    return infer_engine(conn.url, conn.engine)


def get_connection(key: str) -> Connection:
    conns = load_connections()
    if key not in conns:
        available = ", ".join(sorted(conns.keys())) or "<none>"
        err(f"unknown db key '{key}'. Available: {available}", exit_code=EXIT_USAGE)
    return conns[key]


CONN_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _toml_escape_string(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{s}"'


def _is_preservable_field(fv: object) -> bool:
    if isinstance(fv, (str, int, float, bool)):
        return True
    return isinstance(fv, list) and all(isinstance(i, (str, int, float, bool)) for i in fv)


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(item) for item in v) + "]"
    return _toml_escape_string(str(v))


@contextlib.contextmanager
def connections_file_lock():
    """Serialize read-modify-write access to connections.toml across processes.

    Best-effort: on platforms without `fcntl` (e.g. Windows) this is a no-op,
    matching the rest of the codebase's POSIX-first assumptions (docker/ssh/psql).
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX platform
        yield
        return
    lock_path = workspace.WS.connections_file.with_name(workspace.WS.connections_file.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_connections_file_parts() -> tuple[list[str], dict[str, dict[str, object]]]:
    conn_file = workspace.WS.connections_file
    if not conn_file.exists():
        return ([], {})
    text = conn_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    header: list[str] = []
    for line in lines:
        if line.lstrip().startswith("["):
            break
        header.append(line)
    while header and header[-1].strip() == "":
        header.pop()

    with conn_file.open("rb") as f:
        raw = tomllib.load(f)
    data: dict[str, dict[str, object]] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            kept: dict[str, object] = {}
            for fk, fv in v.items():
                if _is_preservable_field(fv):
                    kept[fk] = fv
                else:
                    print(
                        f"warning: connections.toml [{k}].{fk} has an unsupported "
                        "type and will be dropped if this file is rewritten",
                        file=sys.stderr,
                    )
            data[k] = kept
    return (header, data)


def _write_connections_file(header: list[str], data: dict[str, dict[str, object]]) -> None:
    parts: list[str] = []
    if header:
        parts.append("\n".join(header))
        parts.append("")
    field_order = ["url", "engine", "region", "env", "notes"]
    for key, fields in data.items():
        if not CONN_KEY_RE.match(key):
            err(f"invalid connection key '{key}' (must match {CONN_KEY_RE.pattern})", exit_code=EXIT_USAGE)
        parts.append(f"[{key}]")
        emitted: set[str] = set()
        for fk in field_order:
            if fk in fields:
                parts.append(f"{fk:<6} = {_toml_value(fields[fk])}")
                emitted.add(fk)
        for fk, fv in fields.items():
            if fk in emitted:
                continue
            parts.append(f"{fk} = {_toml_value(fv)}")
        parts.append("")
    text = "\n".join(parts).rstrip("\n") + "\n"
    workspace.WS.connections_file.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Query metadata (saved .sql files)
# ---------------------------------------------------------------------------

META_LINE_RE = re.compile(r"^\s*--\s*@([\w-]+)\s*:\s*(.*?)\s*$")
PARAM_RE = re.compile(
    r"^(?P<name>[a-zA-Z_][\w]*)\s*"
    r"(?:\(\s*(?P<type>[\w]+)?\s*"
    r"(?:,\s*(?P<spec>required|default\s*=\s*[^)]*))?\s*\))?\s*$"
)


@dataclass
class Param:
    name: str
    type: str = "text"
    required: bool = False
    default: str | None = None

    def to_meta_value(self) -> str:
        spec_parts: list[str] = [self.type]
        if self.required:
            spec_parts.append("required")
        elif self.default is not None:
            spec_parts.append(f"default={self.default}")
        return f"{self.name} ({', '.join(spec_parts)})"


@dataclass
class Query:
    name: str
    db: str
    desc: str = ""
    tags: list[str] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    schema_sources: list[str] = field(default_factory=list)
    source_fingerprint: str | None = None
    saved_at: str | None = None
    last_validated: str | None = None
    sql: str = ""
    path: Path | None = None

    @property
    def has_limit(self) -> bool:
        return bool(re.search(r"\bLIMIT\b", self.sql, re.IGNORECASE))


def _parse_param_spec(raw: str) -> Param:
    m = PARAM_RE.match(raw)
    if not m:
        err(f"invalid @param spec: {raw!r}", exit_code=EXIT_USAGE)
    name = m.group("name")
    typ = m.group("type") or "text"
    spec = m.group("spec") or ""
    required = False
    default: str | None = None
    if spec.startswith("required"):
        required = True
    elif spec.startswith("default"):
        _, _, val = spec.partition("=")
        default = val.strip()
    return Param(name=name, type=typ, required=required, default=default)


def parse_query_file(path: Path) -> Query:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, list[str]] = {}
    body_lines: list[str] = []
    in_header = True
    for line in text.splitlines():
        if in_header:
            stripped = line.strip()
            if stripped == "" or stripped.startswith("--"):
                m = META_LINE_RE.match(line)
                if m:
                    meta.setdefault(m.group(1), []).append(m.group(2))
                    continue
                if stripped == "" or stripped.startswith("--"):
                    if stripped.startswith("--") and not m:
                        body_lines.append(line)
                    continue
            in_header = False
        body_lines.append(line)

    name_vals = meta.get("name", [])
    db_vals = meta.get("db", [])
    if not name_vals or not db_vals:
        err(f"{path}: missing @name or @db in header", exit_code=EXIT_USAGE)
    if name_vals[0] != path.stem:
        err(
            f"{path}: @name '{name_vals[0]}' does not match filename stem '{path.stem}'",
            exit_code=EXIT_USAGE,
        )

    tags_raw = ",".join(meta.get("tags", []))
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    params = [_parse_param_spec(p) for p in meta.get("param", [])]

    return Query(
        name=name_vals[0],
        db=db_vals[0],
        desc=" ".join(meta.get("desc", [])).strip(),
        tags=tags,
        params=params,
        schema_sources=[s for s in meta.get("schema-source", []) if s],
        source_fingerprint=(meta.get("source-fingerprint") or [None])[0],
        saved_at=(meta.get("saved-at") or [None])[0],
        last_validated=(meta.get("last-validated") or [None])[0],
        sql="\n".join(body_lines).strip(),
        path=path,
    )


def find_query_file(name: str) -> Path:
    matches: list[Path] = []
    for w in workspace.WS_LIST:
        matches += sorted(w.queries_dir.glob(f"**/{name}.sql"))
    if not matches:
        err(f"query '{name}' not found under {workspace.WS.queries_dir}", exit_code=EXIT_USAGE)
    if len(matches) > 1:
        err(
            f"query name '{name}' is ambiguous: {', '.join(str(m) for m in matches)}",
            exit_code=EXIT_USAGE,
        )
    return matches[0]


def load_query(name: str) -> Query:
    return parse_query_file(find_query_file(name))


def list_all_queries() -> list[Query]:
    out: list[Query] = []
    seen: set[str] = set()
    for w in workspace.WS_LIST:
        if not w.queries_dir.exists():
            continue
        for path in sorted(w.queries_dir.glob("**/*.sql")):
            try:
                q = parse_query_file(path)
            except SystemExit:
                raise
            except Exception as exc:
                err(f"failed to parse {path}: {exc}")
                continue
            if q.name in seen:  # earlier workspace wins
                continue
            seen.add(q.name)
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def _resolve_source_path(p: str) -> Path:
    """Resolve a @schema-source value to an absolute path.

    Resolution order:
      1. absolute / ~ -> expand directly
      2. relative to CWD
      3. relative to ~/workspace (common monorepo root)
      4. relative to the workspace home's parent
      5. as-is (caller checks .exists())
    """
    raw = Path(os.path.expanduser(p))
    if raw.is_absolute():
        return raw
    candidates = [
        Path.cwd() / raw,
        Path.home() / "workspace" / raw,
        workspace.WS.home.parent / raw,
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    return raw


def compute_fingerprint(sources: list[str]) -> tuple[str, list[dict[str, Any]]]:
    h = hashlib.sha256()
    details: list[dict[str, Any]] = []
    for declared in sorted(sources):
        resolved = _resolve_source_path(declared)
        if not resolved.exists():
            h.update(f"<MISSING:{declared}>".encode())
            details.append({"declared": declared, "resolved": str(resolved), "exists": False, "size": None})
            continue
        data = resolved.read_bytes()
        size = len(data)
        h.update(f"<FILE:{declared}:{size}>".encode())
        h.update(data)
        details.append({"declared": declared, "resolved": str(resolved), "exists": True, "size": size})
    return ("sha256:" + h.hexdigest()[:16], details)


# ---------------------------------------------------------------------------
# Safety rails (read-only default + auto-limit) — Quarry's AI-native guardrails
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"merge|replace|call|do|vacuum|reindex|cluster|comment|lock|copy)\b",
    re.IGNORECASE,
)
# Data-modifying statements that are legal *inside* a top-level WITH (CTE) and
# would otherwise slip past a leading-keyword check (`WITH d AS (DELETE ...) ...`).
_CTE_WRITE_RE = re.compile(r"\b(insert|update|delete|merge)\b", re.IGNORECASE)
_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)", re.DOTALL)
# Clauses that already bound the row count, or make a trailing `LIMIT` illegal.
_FETCH_RE = re.compile(r"\bFETCH\s+(?:FIRST|NEXT)\b", re.IGNORECASE)
_LOCK_RE = re.compile(r"\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_]\w*)?\$")


def _strip_leading_comments(sql: str) -> str:
    prev = None
    out = sql
    while out != prev:
        prev = out
        out = _LEADING_COMMENT_RE.sub("", out, count=1)
    return out


def sql_skeleton(sql: str) -> str:
    """Blank out comments, string literals, dollar-quoted bodies, and quoted
    identifiers so keyword scanning and `;` splitting can't be fooled by content
    inside them (e.g. `WHERE x = 'DELETE; DROP'` or a column named "limit")."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":            # line comment
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":            # block comment
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        if c == "'":                                                 # string literal
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        if c == '"':                                                 # quoted identifier
            i += 1
            while i < n:
                if sql[i] == '"' and i + 1 < n and sql[i + 1] == '"':
                    i += 2
                    continue
                if sql[i] == '"':
                    i += 1
                    break
                i += 1
            out.append(' "id" ')
            continue
        if c == "$":                                                 # dollar-quoted string
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end == -1 else end + len(tag)
                out.append(" ")
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _statements(sql: str) -> list[str]:
    """Top-level statements (skeleton-split on `;`), empties dropped."""
    return [s for s in sql_skeleton(sql).split(";") if s.strip()]


def is_read_only(sql: str) -> bool:
    """Conservative read-only check.

    Allows a *single* `SELECT` / `WITH ... SELECT` / `SHOW` / `EXPLAIN` / `TABLE`
    / `VALUES` statement. Blocks: any write/DDL by leading keyword, multiple
    statements (`SELECT 1; DROP TABLE t`), and data-modifying CTEs
    (`WITH d AS (DELETE ...) SELECT ...`).
    """
    stmts = _statements(sql)
    if len(stmts) > 1:                       # only one statement may run read-only
        return False
    head = (stmts[0] if stmts else sql_skeleton(sql)).lstrip()
    if _WRITE_RE.match(head):
        return False
    if re.match(r"\s*with\b", head, re.IGNORECASE) and _CTE_WRITE_RE.search(head):
        return False
    return True


def _strip_trailing_semicolons(sql: str) -> str:
    return re.sub(r";\s*$", "", sql.strip())


def has_limit(sql: str) -> bool:
    """True if the query already bounds its rows (LIMIT or FETCH FIRST/NEXT).
    Scans the skeleton so `WHERE x = 'LIMIT'` is not a false positive."""
    sk = sql_skeleton(sql)
    return bool(re.search(r"\bLIMIT\b", sk, re.IGNORECASE) or _FETCH_RE.search(sk))


def enforce_safety(
    sql: str,
    *,
    allow_write: bool,
    max_rows: int | None,
) -> tuple[str, int | None]:
    """Return (possibly-modified sql, applied_limit).

    - Raises QuarryError(EXIT_SAFETY_BLOCKED) on a write/DDL when allow_write=False.
    - When max_rows is set, the statement is read-only, and has no LIMIT, append
      `LIMIT max_rows+1` so the caller can detect truncation. applied_limit is the
      intended row cap (max_rows), else None.
    """
    if not allow_write and not is_read_only(sql):
        raise QuarryError(
            "blocked a write/DDL statement (read-only by default; pass --write to allow)",
            exit_code=EXIT_SAFETY_BLOCKED,
        )
    if max_rows is not None and is_read_only(sql) and not has_limit(sql):
        sk = sql_skeleton(sql)
        cleaned = sk.lstrip()
        # only statements that accept a trailing LIMIT (not EXPLAIN/SHOW/utility
        # output, and not a locking clause which must come after LIMIT)
        if re.match(r"^(select|with|table|values)\b", cleaned, re.IGNORECASE) and not _LOCK_RE.search(sk):
            inner = _strip_trailing_semicolons(sql)
            return (f"{inner}\nLIMIT {max_rows + 1}", max_rows)
    return (sql, None)


# ---------------------------------------------------------------------------
# psql wrapping (postgres engine)
# ---------------------------------------------------------------------------

def _psql_args(url: str) -> list[str]:
    # ON_ERROR_STOP: without it psql -f returns 0 on SQL errors and a failed
    # statement would surface as an empty (successful-looking) result.
    return [resolve_psql(), url, "--no-psqlrc", "--quiet", "--no-align", "--tuples-only",
            "-v", "ON_ERROR_STOP=1"]


def run_psql_capture(
    url: str,
    sql: str,
    *,
    psql_vars: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, str, str]:
    cmd = _psql_args(url)
    for k, v in (psql_vars or {}).items():
        cmd.extend(["-v", f"{k}={v}"])
    cmd.extend(["-f", "-"])
    try:
        proc = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (-1, "", f"psql timed out after {timeout}s")
    return (proc.returncode, proc.stdout, proc.stderr)


def wrap_for_json(sql: str) -> str:
    inner = _strip_trailing_semicolons(sql)
    return (
        "SELECT COALESCE(json_agg(row_to_json(_q_t)), '[]'::json)::text "
        f"FROM ({inner}) _q_t"
    )


def wrap_for_csv(sql: str, with_header: bool = True) -> str:
    inner = _strip_trailing_semicolons(sql)
    return f"COPY ({inner}) TO STDOUT WITH CSV HEADER" if with_header else f"COPY ({inner}) TO STDOUT WITH CSV"


# ---------------------------------------------------------------------------
# MySQL wrapping
# ---------------------------------------------------------------------------


def import_pymysql():
    try:
        import pymysql  # type: ignore[import-not-found]
        return pymysql
    except ImportError:
        err("pymysql not found (pip install pymysql)", exit_code=EXIT_CONNECTION_ERROR)
        raise SystemExit(EXIT_CONNECTION_ERROR)


def parse_mysql_url(url: str) -> dict[str, Any]:
    normalized = re.sub(r"^mysql\+[^:]+://", "mysql://", url.strip(), count=1)
    parsed = urlparse(normalized)
    if parsed.scheme != "mysql":
        err(f"not a mysql URL: {url}", exit_code=EXIT_USAGE)
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        err(f"mysql URL missing database name: {url}", exit_code=EXIT_USAGE)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
    }


_PARAM_RE = re.compile(r":'(\w+)'|:(\w+)")


def substitute_params(sql: str, params: dict[str, str]) -> str:
    """Substitute `:'name'` (quoted+escaped) and `:name` (raw) placeholders in a
    single left-to-right pass, so a substituted value that itself contains a
    `:token` is never re-substituted."""
    def quote_val(value: str) -> str:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"

    def repl(match: re.Match[str]) -> str:
        quoted, raw = match.group(1), match.group(2)
        name = quoted or raw
        if name not in params:
            return match.group(0)
        return quote_val(params[name]) if quoted else str(params[name])

    return _PARAM_RE.sub(repl, sql)


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        elif isinstance(value, date):        # bare date: isoformat() takes no kwargs
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            out[key] = bytes(value).decode("utf-8", errors="replace")
        else:
            out[key] = value
    return out


def run_mysql_query(
    url: str,
    sql: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    pymysql = import_pymysql()
    cfg = parse_mysql_url(url)
    rendered = substitute_params(sql, params or {})
    try:
        conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
            database=cfg["database"], connect_timeout=timeout, read_timeout=timeout,
            write_timeout=timeout, cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.err.MySQLError as exc:
        raise QuarryError(f"mysql connection failed: {exc}", exit_code=EXIT_CONNECTION_ERROR) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(rendered)
            rows = cur.fetchall() if cur.description else []
    except pymysql.err.MySQLError as exc:
        raise QuarryError(f"mysql error: {exc}", exit_code=EXIT_SQL_ERROR) from exc
    finally:
        conn.close()
    return [serialize_row(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Neptune (openCypher over HTTP)
# ---------------------------------------------------------------------------

def normalize_neptune_endpoint(url: str) -> str:
    raw = url.strip()
    if not raw:
        err("empty Neptune endpoint URL", exit_code=EXIT_USAGE)
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        err(f"invalid Neptune endpoint URL: {url}", exit_code=EXIT_USAGE)
    scheme = parsed.scheme or "https"
    if scheme not in {"http", "https"}:
        err(f"unsupported Neptune URL scheme '{scheme}' (expected http/https)", exit_code=EXIT_USAGE)
    port = parsed.port or 8182
    path = parsed.path.rstrip("/")
    base = f"{scheme}://{parsed.hostname}:{port}"
    if path and path != "/":
        base += path
    return base


def _neptune_cypher_url(base_url: str) -> str:
    return base_url if base_url.endswith("/openCypher") else f"{base_url}/openCypher"


def _normalize_row(row: Any) -> dict[str, Any]:
    return row if isinstance(row, dict) else {"value": row}


def _extract_neptune_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_normalize_row(r) for r in payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [_normalize_row(r) for r in payload["results"]]
        if isinstance(payload.get("result"), list):
            return [_normalize_row(r) for r in payload["result"]]
    return [_normalize_row(payload)]


def run_neptune_cypher(
    endpoint_url: str,
    cypher: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = NEPTUNE_TIMEOUT_SEC,
) -> list[dict[str, Any]]:
    rendered = substitute_params(cypher, params or {})
    base = normalize_neptune_endpoint(endpoint_url)
    target = _neptune_cypher_url(base)
    body = urlencode({"query": rendered}).encode("utf-8")
    req = Request(target, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    ssl_context = ssl._create_unverified_context() if NEPTUNE_INSECURE else None
    try:
        with urlopen(req, timeout=timeout, context=ssl_context) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise QuarryError(f"neptune HTTP {exc.code}: {detail}", exit_code=EXIT_SQL_ERROR) from exc
    except URLError as exc:
        raise QuarryError(f"neptune request failed: {exc.reason}", exit_code=EXIT_CONNECTION_ERROR) from exc
    except TimeoutError as exc:
        raise QuarryError(f"neptune request timed out after {timeout}s", exit_code=EXIT_CONNECTION_ERROR) from exc
    try:
        payload = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise QuarryError(f"neptune returned non-JSON body: {raw[:200]}", exit_code=EXIT_SQL_ERROR) from exc
    return _extract_neptune_rows(payload)


# ---------------------------------------------------------------------------
# Structured query API (used by GUI + rich JSON contract)
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    columns: list[dict[str, Any]]   # [{"name": ..., "type": null}]  (types: v2)
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    engine: str
    sql: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "rowCount": self.row_count,
            "truncated": self.truncated,
            "elapsedMs": self.elapsed_ms,
            "engine": self.engine,
            "sql": self.sql,
        }


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: list[str] = []
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.append(k)
    return [{"name": c, "type": None} for c in seen]


_PG_TEXT_STMT_RE = re.compile(r"^\s*(explain|show)\b", re.IGNORECASE)


def _rows_postgres(url: str, sql: str, params: dict[str, str], timeout: int) -> list[dict[str, Any]]:
    # EXPLAIN / SHOW can't live inside a subquery -> run raw, one text row per line.
    cleaned = _strip_leading_comments(sql).lstrip()
    m = _PG_TEXT_STMT_RE.match(cleaned)
    if m:
        rc, out, errout = run_psql_capture(url, sql, psql_vars=params, timeout=timeout)
        if rc != 0:
            raise QuarryError(f"psql failed: {errout.strip()}", exit_code=EXIT_SQL_ERROR)
        col = "QUERY PLAN" if m.group(1).lower() == "explain" else "output"
        return [{col: line} for line in out.rstrip("\n").splitlines()]
    wrapped = wrap_for_json(sql)
    rc, out, errout = run_psql_capture(url, wrapped, psql_vars=params, timeout=timeout)
    if rc != 0:
        raise QuarryError(f"psql failed: {errout.strip()}", exit_code=EXIT_SQL_ERROR)
    text = out.strip() or "[]"
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuarryError(f"postgres returned non-JSON body: {text[:200]}", exit_code=EXIT_SQL_ERROR) from exc


def _pg_column_types(url: str, sql: str, params: dict[str, str], timeout: int = 15) -> dict[str, str]:
    """Real result column types via psql \\gdesc (best-effort; {} on failure)."""
    probe = _strip_trailing_semicolons(sql) + "\n\\gdesc"
    rc, out, _ = run_psql_capture(url, probe, psql_vars=params, timeout=timeout)
    if rc != 0:
        return {}
    types: dict[str, str] = {}
    for line in out.strip().splitlines():
        if "|" in line:
            name, _, typ = line.partition("|")
            types[name.strip()] = typ.strip()
    return types


def run_query(
    conn: Connection,
    sql: str,
    *,
    params: dict[str, str] | None = None,
    allow_write: bool = False,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    timeout: int = 60,
    with_types: bool = False,
) -> QueryResult:
    """Run a query and return a structured QueryResult. The library entry point
    that the GUI and `--format json` rich mode use. Applies the safety rails and
    opens an SSH tunnel when the connection has ssh_host.

    with_types=True fetches real result column types (PostgreSQL only, via \\gdesc);
    other engines leave column types null (the GUI infers from values)."""
    params = params or {}
    engine = connection_engine(conn)
    col_types: dict[str, str] = {}

    # Redis takes a command string, not SQL — use redis-specific safety.
    if engine == "redis":
        if not allow_write and not redis_engine.is_redis_read_only(sql):
            raise QuarryError(
                "blocked a redis write command (read-only by default; pass --write to allow)",
                exit_code=EXIT_SAFETY_BLOCKED,
            )
        start = time.monotonic()
        with tunnel.open_tunnel(conn, engine) as url:
            rows = redis_engine.run_redis(url, sql, timeout=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        applied_limit = max_rows
    else:
        safe_sql, applied_limit = enforce_safety(sql, allow_write=allow_write, max_rows=max_rows)
        sql = safe_sql
        start = time.monotonic()
        with tunnel.open_tunnel(conn, engine) as url:
            if engine == "neptune":
                rows = run_neptune_cypher(url, sql, params=params, timeout=timeout)
            elif engine == "mysql":
                rows = run_mysql_query(url, sql, params=params, timeout=timeout)
            else:
                rows = _rows_postgres(url, sql, params, timeout)
                if with_types:
                    col_types = _pg_column_types(url, sql, params)
        elapsed_ms = int((time.monotonic() - start) * 1000)

    truncated = False
    if applied_limit is not None and len(rows) > applied_limit:
        rows = rows[:applied_limit]
        truncated = True

    columns = _columns_from_rows(rows)
    if col_types:
        for c in columns:
            c["type"] = col_types.get(c["name"])

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        engine=engine,
        sql=sql,
    )


# ---------------------------------------------------------------------------
# Output formatting (CLI presentation)
# ---------------------------------------------------------------------------

def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    fieldnames: list[str] = []
    for row in rows:                       # union of keys — rows may be heterogeneous
        for k in row:
            if k not in fieldnames:
                fieldnames.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _csv_limit(text: str, n: int) -> str:
    """Keep the header + first `n` data rows of CSV text (quote-safe)."""
    parsed = list(csv.reader(io.StringIO(text)))
    if not parsed:
        return text
    buf = io.StringIO()
    csv.writer(buf).writerows(parsed[: 1 + n])
    return buf.getvalue()


def emit_rows_json(rows: list[dict[str, Any]]) -> None:
    if sys.stdout.isatty():
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
    else:
        json.dump(rows, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def emit_rows_ndjson(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        json.dump(row, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


def emit_csv(stdout_text: str) -> None:
    sys.stdout.write(stdout_text)
    if not stdout_text.endswith("\n"):
        sys.stdout.write("\n")


def emit_table(stdout_text: str) -> None:
    reader = csv.reader(io.StringIO(stdout_text))
    rows = list(reader)
    if not rows:
        return
    widths = [max(len(row[i]) if i < len(row) else 0 for row in rows) for i in range(len(rows[0]))]
    sep = "  "
    for idx, row in enumerate(rows):
        padded = [row[i].ljust(widths[i]) if i < len(row) else "".ljust(widths[i]) for i in range(len(rows[0]))]
        print(sep.join(padded))
        if idx == 0:
            print(sep.join("-" * w for w in widths))


def emit_rows_csv(rows: list[dict[str, Any]]) -> None:
    emit_csv(rows_to_csv(rows))


def emit_rows_table(rows: list[dict[str, Any]]) -> None:
    emit_table(rows_to_csv(rows))


def emit_json(stdout_text: str) -> None:
    text = stdout_text.strip() or "[]"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        sys.stdout.write(text + "\n")
        return
    if sys.stdout.isatty():
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        json.dump(data, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


def emit_ndjson(stdout_text: str) -> None:
    text = stdout_text.strip() or "[]"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        sys.stdout.write(text + "\n")
        return
    if not isinstance(data, list):
        data = [data]
    for row in data:
        json.dump(row, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Param resolution
# ---------------------------------------------------------------------------

def parse_kv_args(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            err(f"invalid param '{item}', expected key=value", exit_code=EXIT_USAGE)
        k, _, v = item.partition("=")
        out[k.strip()] = v
    return out


def resolve_params(query: Query, provided: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    declared = {p.name: p for p in query.params}
    for name, p in declared.items():
        if name in provided:
            resolved[name] = provided[name]
        elif p.default is not None:
            resolved[name] = p.default
        elif p.required:
            err(f"missing required param '{name}'", exit_code=EXIT_USAGE)
    for name, val in provided.items():
        if name not in resolved:
            resolved[name] = val
    return resolved


# ---------------------------------------------------------------------------
# execute_sql — CLI path (keeps psql COPY for csv/table; faithful to dbq)
# ---------------------------------------------------------------------------

def _emit_rows(rows: list[dict[str, Any]], fmt: str) -> int:
    if fmt == "json":
        emit_rows_json(rows)
    elif fmt == "ndjson":
        emit_rows_ndjson(rows)
    elif fmt == "csv":
        emit_rows_csv(rows)
    elif fmt == "table":
        emit_rows_table(rows)
    else:
        err(f"unknown format: {fmt}", exit_code=EXIT_USAGE)
    return EXIT_OK


def execute_sql(
    *,
    conn: Connection,
    sql: str,
    psql_vars: dict[str, str],
    fmt: str,
    allow_write: bool = False,
    max_rows: int | None = None,
) -> int:
    engine = connection_engine(conn)

    if engine == "redis":
        if not allow_write and not redis_engine.is_redis_read_only(sql):
            raise QuarryError(
                "blocked a redis write command (read-only by default; pass --write to allow)",
                exit_code=EXIT_SAFETY_BLOCKED,
            )
        with tunnel.open_tunnel(conn, engine) as url:
            rows = redis_engine.run_redis(url, sql)
        return _emit_rows(rows, fmt)

    safe_sql, applied_limit = enforce_safety(sql, allow_write=allow_write, max_rows=max_rows)

    if engine in ("neptune", "mysql"):
        with tunnel.open_tunnel(conn, engine) as url:
            rows = (run_neptune_cypher(url, safe_sql, params=psql_vars) if engine == "neptune"
                    else run_mysql_query(url, safe_sql, params=psql_vars))
        if applied_limit is not None and len(rows) > applied_limit:
            rows = rows[:applied_limit]           # drop the +1 truncation-probe row
        return _emit_rows(rows, fmt)

    with tunnel.open_tunnel(conn, engine) as url:
        if fmt in ("json", "ndjson"):
            rc, out, errout = run_psql_capture(url, wrap_for_json(safe_sql), psql_vars=psql_vars)
            if rc != 0:
                err(f"psql failed: {errout.strip()}", exit_code=EXIT_SQL_ERROR)
            if applied_limit is not None:
                data = json.loads(out.strip() or "[]")
                data = data[:applied_limit] if isinstance(data, list) else data
                emit_rows_json(data) if fmt == "json" else emit_rows_ndjson(data)
            else:
                emit_json(out) if fmt == "json" else emit_ndjson(out)
            return EXIT_OK
        if fmt in ("csv", "table"):
            rc, out, errout = run_psql_capture(url, wrap_for_csv(safe_sql), psql_vars=psql_vars)
            if rc != 0:
                err(f"psql failed: {errout.strip()}", exit_code=EXIT_SQL_ERROR)
            if applied_limit is not None:
                out = _csv_limit(out, applied_limit)
            emit_csv(out) if fmt == "csv" else emit_table(out)
            return EXIT_OK
    err(f"unknown format: {fmt}", exit_code=EXIT_USAGE)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _dummy_value_for(p: Param) -> str:
    t = p.type.lower()
    if t in ("uuid",):
        return "00000000-0000-0000-0000-000000000000"
    if t in ("int", "integer", "bigint", "smallint"):
        return "0"
    if t in ("float", "real", "double", "numeric", "decimal"):
        return "0"
    if t in ("bool", "boolean"):
        return "false"
    if t in ("timestamp", "timestamptz", "date", "time"):
        return "1970-01-01"
    return ""


def validate_query(q: Query, conn: Connection) -> int:
    psql_vars: dict[str, str] = {}
    for p in q.params:
        psql_vars[p.name] = p.default if p.default is not None else _dummy_value_for(p)

    engine = connection_engine(conn)
    # Validation must be side-effect-free: a multi-statement or data-modifying
    # body would otherwise execute its writes under `EXPLAIN <body>`.
    ok = redis_engine.is_redis_read_only(q.sql) if engine == "redis" else is_read_only(q.sql)
    if not ok:
        err("validation failed: query is not read-only (writes/DDL or multiple statements)",
            exit_code=EXIT_SAFETY_BLOCKED)
        return EXIT_SAFETY_BLOCKED

    explain_sql = "EXPLAIN " + _strip_trailing_semicolons(q.sql)
    try:
        with tunnel.open_tunnel(conn, engine) as url:
            if engine == "redis":
                redis_engine.run_redis(url, q.sql, timeout=20)
            elif engine == "neptune":
                run_neptune_cypher(url, q.sql, params=psql_vars, timeout=20)
            elif engine == "mysql":
                run_mysql_query(url, explain_sql, params=psql_vars, timeout=20)
            else:
                rc, _out, errout = run_psql_capture(url, explain_sql, psql_vars=psql_vars, timeout=20)
                if rc != 0:
                    err(f"validation failed: {errout.strip()}")
                    return EXIT_SQL_ERROR
    except Exception as exc:
        err(f"validation failed: {exc}")
        return EXIT_SQL_ERROR
    return EXIT_OK
