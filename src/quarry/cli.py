"""Quarry CLI — the `qy` / `quarry` command-line face over the core engine.

Stable, deterministic. Same args -> same SQL -> same behavior. No LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import core, local, redis_engine, tunnel, workspace
from .core import (
    EXIT_CONNECTION_ERROR,
    EXIT_FINGERPRINT_MISSING,
    EXIT_FINGERPRINT_STALE,
    EXIT_OK,
    EXIT_SQL_ERROR,
    EXIT_STRICT_DRIFT,
    EXIT_USAGE,
    META_LINE_RE,
    Param,
    Query,
    QuarryError,
    compute_fingerprint,
    connection_engine,
    err,
    now_iso,
    parse_query_file,
    resolve_psql,
    run_mysql_query,
    run_neptune_cypher,
    run_psql_capture,
    _strip_trailing_semicolons,
)


# ---------------------------------------------------------------------------
# Param collection (CLI-specific: argparse Namespace -> dict)
# ---------------------------------------------------------------------------

def collect_args_params(args: argparse.Namespace) -> dict[str, str]:
    merged: dict[str, str] = {}
    merged.update(core.parse_kv_args(getattr(args, "params", []) or []))
    merged.update(core.parse_kv_args(getattr(args, "param", []) or []))
    return merged


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------

def cmd_connections_list(args: argparse.Namespace) -> int:
    tree = core.group_connections()
    if args.format == "json":
        json.dump(tree, sys.stdout, indent=2 if sys.stdout.isatty() else None, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    for grp in tree:
        print(f"▸ {grp['group'] or '(ungrouped)'}")
        for item in grp["items"]:
            envs = item["envs"]
            if item["is_env_set"] and (len(envs) > 1 or (envs[0]["env"])):
                env_labels = " ".join(f"[{e['env'] or '?'}]" for e in envs)
                ssh = " · ssh" if any(e["ssh"] for e in envs) else ""
                print(f"    {item['db']}  ({item['engine']}{ssh})  {env_labels}")
            else:
                e = envs[0]
                ssh = " · ssh" if e["ssh"] else ""
                print(f"    {item['db']}  ({item['engine']}{ssh})")
    return EXIT_OK


def _connections_upsert(key, *, url, region, env, notes, engine, require_new) -> None:
    if not core.CONN_KEY_RE.match(key):
        err(f"invalid key '{key}' (letters, digits, underscore; must start with letter)", exit_code=EXIT_USAGE)
    header, data = core._read_connections_file_parts()
    if require_new and key in data:
        err(f"connection '{key}' already exists; use `qy connections set` to update", exit_code=EXIT_USAGE)
    fields: dict[str, str] = {"url": url, "engine": core.infer_engine(url, engine)}
    if region:
        fields["region"] = region
    if env:
        fields["env"] = env
    if notes:
        fields["notes"] = notes
    data[key] = fields
    core._write_connections_file(header, data)


def cmd_connections_add(args: argparse.Namespace) -> int:
    _connections_upsert(args.key, url=args.url, region=args.region, env=args.env,
                        notes=args.notes, engine=args.engine, require_new=True)
    print(f"✓ added connection [{args.key}] → {workspace.WS.connections_file}")
    return EXIT_OK if args.no_test else _connections_test(args.key)


def cmd_connections_set(args: argparse.Namespace) -> int:
    if not core.CONN_KEY_RE.match(args.key):
        err(f"invalid key '{args.key}'", exit_code=EXIT_USAGE)
    header, data = core._read_connections_file_parts()
    existing = data.get(args.key, {})
    fields = dict(existing)
    if args.url is not None:
        fields["url"] = args.url
    if args.region is not None:
        fields["region"] = args.region
    if args.env is not None:
        fields["env"] = args.env
    if args.notes is not None:
        fields["notes"] = args.notes
    if args.engine is not None:
        fields["engine"] = args.engine
    elif "url" in fields and fields["url"] != existing.get("url"):
        fields["engine"] = core.infer_engine(fields["url"], existing.get("engine"))
    if "url" not in fields:
        err("'url' is required for a new connection", exit_code=EXIT_USAGE)
    data[args.key] = fields
    core._write_connections_file(header, data)
    print(f"✓ {'updated' if existing else 'added'} connection [{args.key}] → {workspace.WS.connections_file}")
    return EXIT_OK if args.no_test else _connections_test(args.key)


def cmd_connections_remove(args: argparse.Namespace) -> int:
    header, data = core._read_connections_file_parts()
    if args.key not in data:
        err(f"connection '{args.key}' does not exist", exit_code=EXIT_USAGE)
    if not args.yes:
        sys.stderr.write(f"remove connection [{args.key}]? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            err("aborted", exit_code=EXIT_USAGE)
    del data[args.key]
    core._write_connections_file(header, data)
    print(f"✓ removed connection [{args.key}]")
    return EXIT_OK


def _connections_test(key: str, timeout: int = 10, env: str | None = None) -> int:
    conn = core.resolve_connection(key, env)
    key = conn.key
    engine = connection_engine(conn)
    tag = " (SSH)" if conn.ssh_host else ""
    try:
        with tunnel.open_tunnel(conn, engine) as url:
            if engine == "redis":
                rows = redis_engine.run_redis(url, "PING", timeout=timeout)
                ok = rows and rows[0]["value"].upper() == "PONG"
                if not ok:
                    err("connection test failed: no PONG")
                    return EXIT_CONNECTION_ERROR
                size = redis_engine.run_redis(url, "DBSIZE", timeout=timeout)
                print(f"✓ {key}: connected to Redis{tag} — {size[0]['value'] if size else '?'} keys")
                return EXIT_OK
            if engine == "neptune":
                run_neptune_cypher(url, "RETURN 1 AS ok", timeout=timeout)
                print(f"✓ {key}: connected to Neptune ({core.normalize_neptune_endpoint(conn.url)})")
                return EXIT_OK
            if engine == "mysql":
                rows = run_mysql_query(url, "SELECT DATABASE() AS db_name, VERSION() AS version", timeout=timeout)
                row = rows[0] if rows else {}
                print(f"✓ {key}: connected to {row.get('db_name', '?')} (mysql){tag}")
                if row.get("version"):
                    print(f"  {str(row['version'])[:80]}")
                return EXIT_OK
            rc, out, errout = run_psql_capture(url, "SELECT current_database(), version()", timeout=timeout)
            if rc != 0:
                err(f"connection test failed: {errout.strip()}")
                return EXIT_CONNECTION_ERROR
            parts = [p.strip() for p in out.strip().split("|")]
            print(f"✓ {key}: connected to {parts[0] if parts else '?'}{tag}")
            if len(parts) > 1:
                print(f"  {parts[1][:80]}")
            return EXIT_OK
    except QuarryError as exc:
        err(f"connection test failed: {exc}")
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        err(f"connection test failed: {exc}")
        return EXIT_CONNECTION_ERROR


def cmd_connections_test(args: argparse.Namespace) -> int:
    return _connections_test(args.key, env=getattr(args, "env", None))


# ---------------------------------------------------------------------------
# list / describe / fingerprint / describe-table
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    queries = core.list_all_queries()
    if args.db:
        queries = [q for q in queries if q.db == args.db]
    if args.tag:
        queries = [q for q in queries if args.tag in q.tags]
    if args.format == "json":
        out = [
            {"name": q.name, "db": q.db, "desc": q.desc, "tags": q.tags,
             "params": [p.to_meta_value() for p in q.params], "path": str(q.path) if q.path else None}
            for q in queries
        ]
        json.dump(out, sys.stdout, indent=2 if sys.stdout.isatty() else None, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    if not queries:
        print("(no saved queries)")
        return EXIT_OK
    for q in queries:
        params_str = ", ".join(p.name for p in q.params) or "-"
        print(f"  {q.name}  [{q.db}]")
        if q.desc:
            print(f"      desc:   {q.desc}")
        print(f"      params: {params_str}")
        if q.tags:
            print(f"      tags:   {', '.join(q.tags)}")
    return EXIT_OK


def cmd_describe(args: argparse.Namespace) -> int:
    q = core.load_query(args.name)
    if args.format == "json":
        out = {
            "name": q.name, "db": q.db, "desc": q.desc, "tags": q.tags,
            "params": [{"name": p.name, "type": p.type, "required": p.required, "default": p.default} for p in q.params],
            "schema_sources": q.schema_sources, "source_fingerprint": q.source_fingerprint,
            "saved_at": q.saved_at, "last_validated": q.last_validated,
            "path": str(q.path) if q.path else None, "sql": q.sql,
        }
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    print(f"name:              {q.name}")
    print(f"db:                {q.db}")
    if q.desc:
        print(f"desc:              {q.desc}")
    if q.tags:
        print(f"tags:              {', '.join(q.tags)}")
    print("params:")
    for p in q.params:
        print(f"  - {p.to_meta_value()}")
    if not q.params:
        print("  (none)")
    print("schema-sources:")
    for s in q.schema_sources:
        print(f"  - {s}")
    if not q.schema_sources:
        print("  (none)")
    print(f"fingerprint:       {q.source_fingerprint or '(unset)'}")
    print(f"saved-at:          {q.saved_at or '(unset)'}")
    print(f"last-validated:    {q.last_validated or '(unset)'}")
    print(f"path:              {q.path}")
    print("---- SQL ----")
    print(q.sql)
    return EXIT_OK


def cmd_fingerprint(args: argparse.Namespace) -> int:
    q = core.load_query(args.name)
    if not q.schema_sources:
        result = {"name": q.name, "stale": None, "reason": "no @schema-source declared", "sources": []}
        if args.format == "json":
            json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            print(f"{q.name}: ⚠️  no @schema-source declared (stale=unknown)")
        return EXIT_OK
    actual, details = compute_fingerprint(q.schema_sources)
    any_missing = any(not d["exists"] for d in details)
    stale = (q.source_fingerprint != actual) or any_missing
    result = {
        "name": q.name, "stale": stale, "expected": q.source_fingerprint, "actual": actual,
        "sources": [{**d, "match": (d["exists"] and not stale)} for d in details],
    }
    if args.format == "json":
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(f"{q.name}: {'✗ stale' if stale else '✓ fresh'}")
        print(f"  expected: {q.source_fingerprint}")
        print(f"  actual:   {actual}")
        for d in details:
            tag = "MISSING" if not d["exists"] else f"{d['size']} bytes"
            print(f"  - {d['declared']}  [{tag}]")
    if any_missing:
        return EXIT_FINGERPRINT_MISSING
    if stale:
        return EXIT_FINGERPRINT_STALE
    return EXIT_OK


def cmd_describe_table(args: argparse.Namespace) -> int:
    conn = core.resolve_connection(args.db_key, getattr(args, "env", None))
    engine = connection_engine(conn)
    if engine in ("neptune", "redis"):
        err(f"describe-table is not supported for engine={engine}", exit_code=EXIT_USAGE)
    with tunnel.open_tunnel(conn, engine) as url:
        if engine == "mysql":
            sql = """
                SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = :'table'
                ORDER BY ordinal_position
            """
            try:
                rows = run_mysql_query(url, sql, params={"table": args.table}, timeout=15)
            except Exception as exc:
                err(f"mysql failed: {exc}", exit_code=EXIT_SQL_ERROR)
            if args.format == "text":
                if not rows:
                    print(f"(table {args.table} not found or has no columns)")
                    return EXIT_OK
                headers = list(rows[0].keys())
                widths = [max(len(str(row.get(h, ""))) for row in rows + [{h: h for h in headers}]) for h in headers]
                print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
                print("  ".join("-" * w for w in widths))
                for row in rows:
                    print("  ".join(str(row.get(h, "")).ljust(widths[i]) for i, h in enumerate(headers)))
                return EXIT_OK
            json.dump({"table": args.table, "columns": rows}, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
            return EXIT_OK

        if args.format == "text":
            cmd = [resolve_psql(), url, "--no-psqlrc", "-c", f'\\d+ "{args.table}"']
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                err("psql timed out", exit_code=EXIT_CONNECTION_ERROR)
                return EXIT_CONNECTION_ERROR
            if proc.returncode != 0:
                err(f"psql failed: {proc.stderr.strip()}", exit_code=EXIT_SQL_ERROR)
            sys.stdout.write(proc.stdout)
            return EXIT_OK

        sql = f"""
            SELECT json_agg(row_to_json(c))::text FROM (
                SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
                FROM information_schema.columns
                WHERE table_name = '{args.table}'
                ORDER BY ordinal_position
            ) c
        """
        rc, out, errout = run_psql_capture(url, sql, timeout=15)
        if rc != 0:
            err(f"psql failed: {errout.strip()}", exit_code=EXIT_SQL_ERROR)
        text = out.strip() or "[]"
        json.dump({"table": args.table, "columns": json.loads(text)}, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK


# ---------------------------------------------------------------------------
# exec / run
# ---------------------------------------------------------------------------

def _confirm_prod_write(conn, sql, args) -> bool:
    """prod safety: a write against a prod connection needs explicit confirmation."""
    if (conn.env or "").lower() != "prod":
        return True
    if not getattr(args, "write", False) or core.is_read_only(sql):
        return True
    if getattr(args, "yes", False):
        return True
    sys.stderr.write(f"⚠️  write against PROD ({conn.key}). Proceed? [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _read_sql_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        err(f"cannot read --file {path}: {exc.strerror or exc}", exit_code=EXIT_USAGE)
        raise SystemExit(EXIT_USAGE)  # unreachable (err raises)


def _execute(conn, sql, psql_vars, args) -> int:
    if not _confirm_prod_write(conn, sql, args):
        err("aborted", exit_code=EXIT_USAGE)
    try:
        return core.execute_sql(
            conn=conn, sql=sql, psql_vars=psql_vars, fmt=args.format,
            allow_write=getattr(args, "write", False),
            max_rows=getattr(args, "max_rows", None),
        )
    except QuarryError as exc:
        err(str(exc), exit_code=exc.exit_code)
        return exc.exit_code


# Match only a real trailing-style LIMIT clause (count / ALL / :param), optionally
# with OFFSET — never spanning parens, ORDER BY, or other clauses.
_LIMIT_CLAUSE_RE = re.compile(r"\bLIMIT\s+(?:\d+|ALL|:\w+)(?:\s+OFFSET\s+\d+)?", re.IGNORECASE)


def _paren_depths(sql: str) -> list[int]:
    """Paren depth at each character index, ignoring parens inside strings/comments."""
    depths = [0] * len(sql)
    depth, i, n = 0, 0, len(sql)
    in_s = in_line = in_block = False
    while i < n:
        c = sql[i]
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and i + 1 < n and sql[i + 1] == "/":
                in_block = False
                depths[i] = depth
                i += 1
        elif in_s:
            if c == "'":
                in_s = False
        elif c == "'":
            in_s = True
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            in_line = True
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            in_block = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        depths[i] = depth
        i += 1
    return depths


def _last_toplevel_limit(sql: str) -> re.Match[str] | None:
    """The last LIMIT clause sitting at paren depth 0 (the outer/result LIMIT)."""
    depths = _paren_depths(sql)
    found = None
    for m in _LIMIT_CLAUSE_RE.finditer(sql):
        if depths[m.start()] == 0:
            found = m
    return found


def _override_limit(sql: str, limit: int) -> str:
    """Replace the outer LIMIT clause with `LIMIT <limit>`, else append one."""
    m = _last_toplevel_limit(sql)
    if m:
        return sql[: m.start()] + f"LIMIT {limit}" + sql[m.end():]
    return _strip_trailing_semicolons(sql) + f"\nLIMIT {limit}"


def _strip_limit(sql: str) -> str:
    """Remove the outer LIMIT[/OFFSET] clause (for --full)."""
    m = _last_toplevel_limit(sql)
    if not m:
        return sql
    return (sql[: m.start()] + sql[m.end():]).rstrip()


def cmd_exec(args: argparse.Namespace) -> int:
    conn = core.resolve_connection(args.db_key, getattr(args, "env", None))
    psql_vars = collect_args_params(args)
    sql = args.sql
    if not sql and args.file:
        sql = _read_sql_file(args.file)
    if not sql:
        err("must provide --sql or --file", exit_code=EXIT_USAGE)
    return _execute(conn, sql, psql_vars, args)


def cmd_run(args: argparse.Namespace) -> int:
    q = core.load_query(args.name)
    conn = core.resolve_connection(q.db, getattr(args, "env", None))
    psql_vars = core.resolve_params(q, collect_args_params(args))
    sql = q.sql
    if args.full:
        sql = _strip_limit(sql)
    elif args.limit is not None:
        sql = _override_limit(sql, args.limit)
    if args.strict:
        rc = core.validate_query(q, conn)
        if rc != EXIT_OK:
            err(f"strict mode: query '{q.name}' failed validation", exit_code=EXIT_STRICT_DRIFT)
    return _execute(conn, sql, psql_vars, args)


# ---------------------------------------------------------------------------
# save / validate / remove / edit / audit
# ---------------------------------------------------------------------------

def _parse_param_cli(spec: str) -> Param:
    parts = spec.split(":")
    if not parts:  # pragma: no cover  (str.split never returns []; defensive only)
        err(f"invalid --param: {spec!r}", exit_code=EXIT_USAGE)
    name = parts[0].strip()
    typ = parts[1].strip() if len(parts) >= 2 else "text"
    rest = ":".join(parts[2:]).strip() if len(parts) >= 3 else ""
    required = False
    default: str | None = None
    if rest == "required":
        required = True
    elif rest.startswith("default="):
        default = rest[len("default="):]
    elif rest:
        err(f"unknown qualifier in --param: {rest!r}", exit_code=EXIT_USAGE)
    return Param(name=name, type=typ, required=required, default=default)


def _format_query_file(q: Query) -> str:
    lines: list[str] = [f"-- @name: {q.name}", f"-- @db: {q.db}"]
    if q.desc:
        lines.append(f"-- @desc: {q.desc}")
    if q.tags:
        lines.append(f"-- @tags: {', '.join(q.tags)}")
    for p in q.params:
        lines.append(f"-- @param: {p.to_meta_value()}")
    for s in q.schema_sources:
        lines.append(f"-- @schema-source: {s}")
    if q.source_fingerprint:
        lines.append(f"-- @source-fingerprint: {q.source_fingerprint}")
    if q.saved_at:
        lines.append(f"-- @saved-at: {q.saved_at}")
    if q.last_validated:
        lines.append(f"-- @last-validated: {q.last_validated}")
    lines.append("")
    lines.append(q.sql.rstrip() + ("" if q.sql.rstrip().endswith(";") else ";"))
    lines.append("")
    return "\n".join(lines)


def cmd_save(args: argparse.Namespace) -> int:
    if not args.sql and not args.file:
        err("must provide --sql or --file", exit_code=EXIT_USAGE)
    sql = (args.sql if args.sql else _read_sql_file(args.file)).strip()
    if not sql:
        err("SQL is empty", exit_code=EXIT_USAGE)
    conn = core.resolve_connection(args.db)
    target_dir = workspace.WS.queries_dir / args.db
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{args.name}.sql"
    if target.exists() and not args.overwrite:
        err(f"query '{args.name}' already exists at {target}; use --overwrite", exit_code=EXIT_USAGE)
    params = [_parse_param_cli(p) for p in (args.param or [])]
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    fingerprint, _ = compute_fingerprint(args.schema_source) if args.schema_source else (None, [])
    q = Query(name=args.name, db=args.db, desc=args.desc, tags=tags, params=params,
              schema_sources=list(args.schema_source or []), source_fingerprint=fingerprint,
              saved_at=now_iso(), last_validated=None, sql=sql)
    target.write_text(_format_query_file(q), encoding="utf-8")
    print(f"✓ saved {target}")
    if not args.no_validate:
        rc = core.validate_query(parse_query_file(target), conn)
        if rc != EXIT_OK:
            err(f"warning: saved but validation failed for '{args.name}'")
            return rc
        _stamp_validated(target, q.saved_at)
        print(f"✓ validated against {q.db}")
    return EXIT_OK


def _stamp_validated(target: Path, saved_at: str | None) -> None:
    text = target.read_text(encoding="utf-8")
    new_ts = now_iso()
    if "@last-validated" in text:
        new_text = re.sub(r"^(\s*--\s*@last-validated\s*:\s*).*$", rf"\g<1>{new_ts}",
                          text, count=1, flags=re.MULTILINE)
    elif saved_at:
        new_text = text.replace(f"-- @saved-at: {saved_at}",
                                f"-- @saved-at: {saved_at}\n-- @last-validated: {new_ts}", 1)
    else:
        new_text = text
    target.write_text(new_text, encoding="utf-8")


def cmd_validate(args: argparse.Namespace) -> int:
    q = core.load_query(args.name)
    conn = core.resolve_connection(q.db)
    rc = core.validate_query(q, conn)
    if rc != EXIT_OK:
        return rc
    if q.path:
        text = q.path.read_text(encoding="utf-8")
        new_ts = now_iso()
        if "@last-validated" in text:
            new_text = re.sub(r"^(\s*--\s*@last-validated\s*:\s*).*$", rf"\g<1>{new_ts}",
                              text, count=1, flags=re.MULTILINE)
        else:
            lines = text.splitlines()
            insert_idx = 0
            for i, line in enumerate(lines):
                if META_LINE_RE.match(line):
                    insert_idx = i + 1
            lines.insert(insert_idx, f"-- @last-validated: {new_ts}")
            new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        q.path.write_text(new_text, encoding="utf-8")
    print(f"✓ {q.name}: schema OK")
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    path = core.find_query_file(args.name)
    if not args.yes:
        sys.stderr.write(f"remove {path}? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            err("aborted", exit_code=EXIT_USAGE)
    path.unlink()
    print(f"✓ removed {path}")
    return EXIT_OK


def cmd_edit(args: argparse.Namespace) -> int:
    path = core.find_query_file(args.name)
    editor = os.environ.get("EDITOR", "vi")
    return subprocess.call([editor, str(path)])


def cmd_workspace_list(args: argparse.Namespace) -> int:
    ws = workspace.config_workspaces()
    active = [str(w.home) for w in workspace.WS_LIST]
    if getattr(args, "format", "text") == "json":
        json.dump({"config": str(workspace._config_path()), "workspaces": ws, "active": active},
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return EXIT_OK
    if not ws:
        print("(config.toml 里没有 workspace;当前用 --workspace/$QUARRY_WORKSPACE 或 cwd)")
    for w in ws:
        home = Path(w).expanduser()
        ok = "✓" if home.exists() else "✗"
        conn = "" if (home / "connections.toml").exists() else "  (无 connections.toml)"
        print(f"  {ok} {w}{conn}")
    print(f"config: {workspace._config_path()}")
    print(f"当前生效: {', '.join(active)}")
    return EXIT_OK


def cmd_workspace_add(args: argparse.Namespace) -> int:
    added, cfg = workspace.add_workspace(args.dir)
    home = Path(args.dir).expanduser()
    if not home.exists():
        print(f"⚠️  目录不存在: {home}(仍已记录)")
    elif not (home / "connections.toml").exists():
        print(f"⚠️  {home} 下没有 connections.toml")
    print(f"✓ 已加入 workspace: {args.dir} → {cfg}" if added else f"已存在,未重复添加: {args.dir}")
    workspace.configure_workspace(None)
    return cmd_workspace_list(argparse.Namespace(format="text"))


def cmd_workspace_remove(args: argparse.Namespace) -> int:
    if not workspace.remove_workspace(args.dir):
        err(f"config.toml 里未找到该 workspace: {args.dir}", exit_code=EXIT_USAGE)
    print(f"✓ 已移除: {args.dir}")
    return EXIT_OK


def cmd_gui(args: argparse.Namespace) -> int:
    from . import gui
    # workspace already configured in main(); pass through so the server keeps it.
    return gui.serve(host=args.host, port=args.port, ws_path=args.workspace,
                     open_browser=not args.no_open)


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp
    return mcp.serve(args.workspace, allow_write=args.write)


def cmd_audit(args: argparse.Namespace) -> int:
    queries = core.list_all_queries()
    rows: list[dict[str, Any]] = []
    for q in queries:
        if not q.schema_sources:
            rows.append({"name": q.name, "db": q.db, "status": "no-source", "detail": ""})
            continue
        actual, details = compute_fingerprint(q.schema_sources)
        any_missing = any(not d["exists"] for d in details)
        if any_missing:
            rows.append({"name": q.name, "db": q.db, "status": "missing-source",
                         "detail": ", ".join(d["declared"] for d in details if not d["exists"])})
        elif actual != q.source_fingerprint:
            rows.append({"name": q.name, "db": q.db, "status": "stale",
                         "detail": f"expected {q.source_fingerprint}, got {actual}"})
        else:
            rows.append({"name": q.name, "db": q.db, "status": "fresh", "detail": q.last_validated or ""})
    if args.format == "json":
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    if not rows:
        print("(no saved queries)")
        return EXIT_OK
    icons = {"fresh": "✓", "stale": "✗", "missing-source": "✗", "no-source": "⚠"}
    name_w = max(len(r["name"]) for r in rows)
    db_w = max(len(r["db"]) for r in rows)
    for r in rows:
        print(f"  {icons.get(r['status'], '?')} {r['name']:<{name_w}}  [{r['db']:<{db_w}}]  {r['status']:<14}  {r['detail']}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# local — dev containers (qy local up/down/status)
# ---------------------------------------------------------------------------

def _resolve_local_target(arg: str, engine_flag: str | None) -> tuple[str, local.EngineSpec, str | None]:
    """Map an `up <key>` argument to (logical_db, EngineSpec, group).

    If the argument matches an existing connection (by key or logical db) the
    engine + group come from it; otherwise it is treated as a brand-new logical
    db and the engine comes from --engine (default postgres)."""
    try:
        conns = core.load_connections()
    except QuarryError:
        conns = {}
    # exact key match wins over a logical-db match that may sit earlier in the file
    match = conns.get(arg)
    if match is None:
        for c in conns.values():
            if c.logical_db == arg:
                match = c
                break
    if match is not None:
        logical = match.logical_db
        eng = connection_engine(match)
        if eng not in local.SPECS:
            err(f"engine '{eng}' has no local-container support (postgres/redis only)",
                exit_code=EXIT_USAGE)
        if engine_flag not in (None, "all") and engine_flag != eng:
            err(f"connection '{arg}' is engine {eng}, not {engine_flag}", exit_code=EXIT_USAGE)
        spec = local.SPECS[eng]
        group = match.group
    else:
        logical = arg
        eng = engine_flag if engine_flag not in (None, "all") else "postgres"
        spec = local.SPECS[eng]
        group = None
    if not local.SAFE_DB_RE.match(logical):
        err(f"'{logical}' is not a valid local db name (letters, digits, underscore; "
            "must start with a letter)", exit_code=EXIT_USAGE)
    return logical, spec, group


def cmd_local_up(args: argparse.Namespace) -> int:
    if args.key:
        logical, spec, group = _resolve_local_target(args.key, args.engine)
        image = args.image or local.stored_local_image(logical)
        state = local.start_container(spec, image=image)
        print(f"✓ local {spec.engine} container {_state_word(state)} "
              f"(port {spec.port}, image {image or spec.default_image})")
        if spec.engine == "postgres":
            if not local.wait_pg_ready(spec):
                err("local postgres did not become ready in time", exit_code=EXIT_CONNECTION_ERROR)
            local.ensure_pg_database(spec, logical)
        key, created = local.register_local_connection(
            logical, spec, image=args.image, group=group)
        if created:
            print(f"✓ registered connection [{key}] (env=local) → {workspace.WS.connections_file}")
        else:
            print(f"· connection [{key}] (env=local) already registered — left unchanged")
        return EXIT_OK

    for spec in local.specs_for(args.engine):
        state = local.start_container(spec, image=args.image)
        print(f"✓ local {spec.engine} container {_state_word(state)} "
              f"(port {spec.port}, image {args.image or spec.default_image})")
    return EXIT_OK


def _state_word(state: str) -> str:
    return {"running": "already running", "started": "started",
            "created": "created"}.get(state, state)


def cmd_local_down(args: argparse.Namespace) -> int:
    for spec in local.specs_for(args.engine):
        res = local.down_engine(spec, purge=args.purge)
        if res["was"] == "absent":
            print(f"· local {spec.engine} container not present")
            if res["removed_volume"]:
                print(f"✓ removed volume {spec.volume}")
            continue
        action = "stopped" if res["stopped"] else "already stopped"
        line = f"✓ local {spec.engine} container {action}"
        if args.purge:
            line += " and removed"
        print(line)
        if args.purge:
            if res["removed_volume"]:
                print(f"✓ removed volume {spec.volume} (local data destroyed)")
            else:
                print(f"· volume {spec.volume} did not exist")
    return EXIT_OK


def cmd_local_status(args: argparse.Namespace) -> int:
    statuses = [local.engine_status(spec) for spec in local.specs_for(args.engine)]
    if args.format == "json":
        json.dump(statuses, sys.stdout, indent=2 if sys.stdout.isatty() else None,
                  ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    for st in statuses:
        if not st["docker"]:
            print(f"  ? {st['engine']:<9} docker unavailable")
            continue
        if st["running"]:
            print(f"  ✓ {st['engine']:<9} running  port {st['port']}  image {st['image']}")
        else:
            data_note = " (data volume present)" if st["volume_exists"] else ""
            print(f"  ✗ {st['engine']:<9} not running{data_note} — "
                  f"run `qy local up --engine {st['engine']}`")
    return EXIT_OK


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------

def _add_safety_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env", default=None, help="Target env for an env-set (dev/prod/jp; default: dev)")
    p.add_argument("--write", action="store_true", help="Allow write/DDL (off by default — read-only)")
    p.add_argument("--yes", action="store_true", help="Skip the prod-write confirmation prompt")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows when SQL has no LIMIT (safety; default: unlimited for CLI)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qy",
        description="Quarry — multi-engine database query tool (PostgreSQL, MySQL, Neptune)",
    )
    parser.add_argument("--workspace", default=None,
                        help="Workspace dir (connections.toml + queries/); overrides $QUARRY_WORKSPACE")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    p_conn = sub.add_parser("connections", help="Manage DB connections (list/add/set/remove/test)")
    p_conn.add_argument("--format", choices=["table", "json"], default="table")
    p_conn.set_defaults(func=cmd_connections_list)
    conn_sub = p_conn.add_subparsers(dest="conn_cmd", metavar="<subcommand>")
    p_cl = conn_sub.add_parser("list", help="List connections (default)")
    p_cl.add_argument("--format", choices=["table", "json"], default="table")
    p_cl.set_defaults(func=cmd_connections_list)
    p_ca = conn_sub.add_parser("add", help="Add a new connection (errors if exists)")
    p_ca.add_argument("key")
    p_ca.add_argument("--url", required=True)
    p_ca.add_argument("--engine", choices=["postgres", "mysql", "neptune"], default=None)
    p_ca.add_argument("--region", default=None)
    p_ca.add_argument("--env", default=None)
    p_ca.add_argument("--notes", default=None)
    p_ca.add_argument("--no-test", action="store_true")
    p_ca.set_defaults(func=cmd_connections_add)
    p_cs = conn_sub.add_parser("set", help="Add or update a connection (upsert)")
    p_cs.add_argument("key")
    p_cs.add_argument("--url", default=None)
    p_cs.add_argument("--engine", choices=["postgres", "mysql", "neptune"], default=None)
    p_cs.add_argument("--region", default=None)
    p_cs.add_argument("--env", default=None)
    p_cs.add_argument("--notes", default=None)
    p_cs.add_argument("--no-test", action="store_true")
    p_cs.set_defaults(func=cmd_connections_set)
    p_cr = conn_sub.add_parser("remove", help="Delete a connection")
    p_cr.add_argument("key")
    p_cr.add_argument("--yes", action="store_true")
    p_cr.set_defaults(func=cmd_connections_remove)
    p_ct = conn_sub.add_parser("test", help="Test connectivity")
    p_ct.add_argument("key")
    p_ct.add_argument("--env", default=None, help="Target env for an env-set")
    p_ct.set_defaults(func=cmd_connections_test)

    p = sub.add_parser("list", help="List saved named queries")
    p.add_argument("--db")
    p.add_argument("--tag")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("describe", help="Show metadata + SQL for a saved query")
    p.add_argument("name")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_describe)

    for alias in ("describe-table", "schema"):
        p = sub.add_parser(alias, help="Live DB schema for a table (\\d+)")
        p.add_argument("db_key")
        p.add_argument("table")
        p.add_argument("--env", default=None, help="Target env for an env-set")
        p.add_argument("--format", choices=["text", "json"], default="text")
        p.set_defaults(func=cmd_describe_table)

    p = sub.add_parser("run", help="Run a saved query")
    p.add_argument("name")
    p.add_argument("params", nargs="*")
    p.add_argument("--param", action="append", default=[])
    p.add_argument("--format", choices=["json", "ndjson", "csv", "table"], default="json")
    p.add_argument("--limit", type=int)
    p.add_argument("--full", action="store_true")
    p.add_argument("--strict", action="store_true")
    _add_safety_flags(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("exec", help="Execute ad-hoc SQL against a db")
    p.add_argument("db_key")
    p.add_argument("--sql")
    p.add_argument("--file")
    p.add_argument("params", nargs="*")
    p.add_argument("--param", action="append", default=[])
    p.add_argument("--format", choices=["json", "ndjson", "csv", "table"], default="json")
    _add_safety_flags(p)
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("save", help="Save a named query")
    p.add_argument("name")
    p.add_argument("--db", required=True)
    p.add_argument("--desc", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--param", action="append", default=[])
    p.add_argument("--schema-source", action="append", default=[])
    p.add_argument("--sql")
    p.add_argument("--file")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-validate", action="store_true")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("validate", help="EXPLAIN-validate a saved query")
    p.add_argument("name")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("fingerprint", help="Check schema-source fingerprint freshness")
    p.add_argument("name")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("audit", help="Freshness report for all saved queries")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("remove", help="Delete a saved query")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("edit", help="Open a saved query in $EDITOR")
    p.add_argument("name")
    p.set_defaults(func=cmd_edit)

    p_ws = sub.add_parser("workspace", help="Manage aggregated workspaces (config.toml)")
    p_ws.set_defaults(func=cmd_workspace_list, format="text")
    ws_sub = p_ws.add_subparsers(dest="ws_cmd", metavar="<subcommand>")
    p_wl = ws_sub.add_parser("list", help="List configured workspaces (default)")
    p_wl.add_argument("--format", choices=["text", "json"], default="text")
    p_wl.set_defaults(func=cmd_workspace_list)
    p_wa = ws_sub.add_parser("add", help="Add a workspace dir to config.toml")
    p_wa.add_argument("dir")
    p_wa.set_defaults(func=cmd_workspace_add)
    p_wr = ws_sub.add_parser("remove", help="Remove a workspace dir from config.toml")
    p_wr.add_argument("dir")
    p_wr.set_defaults(func=cmd_workspace_remove)

    p_local = sub.add_parser(
        "local", help="Manage local dev containers (postgres/redis) for env=local connections")
    local_sub = p_local.add_subparsers(dest="local_cmd", metavar="<subcommand>", required=True)
    p_lu = local_sub.add_parser(
        "up", help="Start local container(s); with a key, auto-register an env=local connection")
    p_lu.add_argument("key", nargs="?",
                      help="Connection key / logical db to bring up + auto-register (env=local)")
    p_lu.add_argument("--engine", choices=["postgres", "redis", "all"], default=None)
    p_lu.add_argument("--image", default=None, help="Override the container image tag")
    p_lu.set_defaults(func=cmd_local_up)
    p_ld = local_sub.add_parser(
        "down", help="Stop local container(s); --purge also deletes the data volume")
    p_ld.add_argument("--engine", choices=["postgres", "redis", "all"], default=None)
    p_ld.add_argument("--purge", action="store_true",
                      help="Also delete the named data volume (destroys local data)")
    p_ld.set_defaults(func=cmd_local_down)
    p_ls = local_sub.add_parser("status", help="Show local container status (running / port / image)")
    p_ls.add_argument("--engine", choices=["postgres", "redis", "all"], default=None)
    p_ls.add_argument("--format", choices=["text", "json"], default="text")
    p_ls.set_defaults(func=cmd_local_status)

    p = sub.add_parser("gui", help="Launch the local data-viewer GUI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("mcp", help="Serve the MCP face over stdio (for AI agents)")
    p.add_argument("--write", action="store_true",
                   help="Allow tool calls to request writes (per-call opt-in still required)")
    p.set_defaults(func=cmd_mcp)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    workspace.configure_workspace(args.workspace)
    try:
        return args.func(args)
    except QuarryError as exc:
        print(f"quarry: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.dup2(devnull, sys.stderr.fileno())
        except Exception:
            pass
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
