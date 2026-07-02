# Quarry

> **The database workbench built for the AI era** — one kernel, many faces (CLI / GUI / MCP / agent skill).

[![CI](https://github.com/Wangggym/quarry/actions/workflows/ci.yml/badge.svg)](https://github.com/Wangggym/quarry/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/quarry-db)](https://pypi.org/project/quarry-db/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/quarry-db/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文文档 →](README.zh-CN.md) · [Website →](https://quarry.yiminlab.site)

![Quarry demo](site/assets/demo.svg)

Every database tool you know — DBeaver, TablePlus, pgAdmin — assumes a *human* at the keyboard. But increasingly, the entity running your queries is an **AI agent**, and agents need different guarantees:

- **Results a machine can parse**, not a screen a human can read
- **Safety rails that live in the kernel**, so no client can forget them
- **Deterministic error contracts** (stable exit codes), not stack traces to scrape
- **Configuration as files**, not clicks — so it can be versioned, diffed, and shared with agents

Quarry inverts the traditional design: it is a **query kernel with an agent-safe contract first**, and the human faces (CLI, GUI) are thin shells grown from the same kernel. Whether a query comes from a person in the browser, a script in CI, or Claude running a skill, it passes through the exact same safety rails and returns the exact same structured result.

## Philosophy

1. **One core, many faces.** Connection management, query execution, schema introspection, and safety rails live in an importable kernel (`quarry.core`). The CLI (`qy`), the GUI, the MCP server, and agent skills are thin shells. Fix a bug once, every face gets it.

2. **Read-only by default; escalation is explicit and graduated.** Writes and DDL are blocked (exit code `8`) unless you pass `--write`. Production connections require an *additional* confirmation on top of `--write`. Every query gets an automatic `LIMIT 500` unless you opt out. Because the rails are in the kernel, an agent cannot bypass them by picking a different entry point.

3. **A contract machines can trust.** Every query returns `{columns, rows, rowCount, truncated, elapsedMs, engine, sql}`. Exit codes are stable API: `0` ok, `2` connection error, `3` SQL error, `8` safety block. An agent can branch on outcomes without parsing prose.

4. **Workspace as code.** A workspace is just a directory: `connections.toml` + `queries/**/*.sql` (named queries with `-- @meta` headers). It lives in *your* repo, versioned by git, shared between teammates and agents alike. The kernel itself carries zero business logic and zero secrets.

5. **Nearly zero dependencies.** Pure stdlib. PostgreSQL goes through your system `psql`, Redis through `redis-cli`, SSH tunnels through system `ssh`. MySQL is one optional `pymysql`. No Electron, no daemon, no cloud.

## Install

```bash
pipx install quarry-db        # or: pip install quarry-db
qy --help
```

PostgreSQL uses the system `psql` binary; MySQL needs `pip install "quarry-db[mysql]"`.

## Quickstart

```bash
mkdir my-workspace && cd my-workspace
cat > connections.toml <<'EOF'
[shop]
url    = "postgresql://user:pass@localhost:5432/shop"
engine = "postgres"
env    = "dev"
EOF

qy connections                       # list connections
qy exec shop --sql "select * from customers"
qy schema shop customers             # table structure (\d+)
qy gui                               # browser data grid
```

## Workspace

A workspace directory is the source of connections + queries:

```
my-workspace/
├── connections.toml      # [key] url / engine / env / group / notes
└── queries/<db>/*.sql    # named queries (with -- @meta headers)
```

Resolution order: `--workspace PATH` → `~/.config/quarry/config.toml` → current directory.

## CLI reference

| Command | Purpose |
|---------|---------|
| `qy connections [list\|add\|set\|remove\|test]` | Manage connections |
| `qy exec <db> --sql "..." [--format json\|ndjson\|csv\|table]` | Run ad-hoc SQL |
| `qy schema <db> <table>` | Live table structure |
| `qy run <name> [k=v ...]` | Run a saved named query |
| `qy save <name> --db X --sql "..."` | Save a named query |
| `qy list / describe / validate / fingerprint / audit` | Manage named queries |
| `qy workspace list/add/remove` | Manage aggregated workspaces |
| `qy gui` | Launch the local GUI |
| `qy mcp [--write]` | Serve the MCP face over stdio (for AI agents) |

## MCP (the agent-native face)

`qy mcp` speaks the Model Context Protocol over stdio — pure stdlib, no SDK dependency. Agents get six tools (`list_connections`, `list_tables`, `describe_table`, `exec_sql`, `list_saved_queries`, `run_saved_query`) with the exact same kernel rails: read-only unless the server was started with `--write` *and* the call passes `write: true`; a prod env additionally requires `confirm_prod: true`.

```bash
# Claude Code
claude mcp add quarry -- qy mcp --workspace ~/my-workspace
```

```json
// or any MCP client (.mcp.json)
{ "mcpServers": { "quarry": { "command": "qy", "args": ["mcp", "--workspace", "/path/to/workspace"] } } }
```

## Safety rails (the AI-native moat)

- **Read-only by default**: writes/DDL blocked with exit code `8`; `--write` to allow
- **Automatic row cap**: `run_query()` injects `LIMIT 500`; raise with `--max-rows N`
- **Graduated prod protection**: all envs default read-only → dev needs `--write` → prod needs `--write` *plus* an interactive confirmation (`--yes` for automation)
- **Stable exit-code contract**: `0` ok / `2` connection / `3` SQL / `8` safety block

## As a library (what the GUI and agents use)

```python
from quarry import configure_workspace, get_connection, run_query

configure_workspace("~/my-workspace")
res = run_query(get_connection("shop"), "select * from customers")
print(res.to_dict())   # {columns, rows, rowCount, truncated, elapsedMs, engine, sql}
```

## SSH tunnels

For databases only reachable via a bastion, add `ssh_*` fields and `qy` opens the tunnel automatically (system `ssh`, zero dependencies):

```toml
[internal_db]
url      = "postgresql://user:pass@127.0.0.1:5432/appdb"
engine   = "postgres"
ssh_host = "bastion.example.com"
ssh_user = "ubuntu"
ssh_key  = "~/.ssh/id_ed25519"
```

## Redis

`engine = "redis"` (uses system `redis-cli`). Queries are redis commands:

```bash
qy exec cache --sql "SCAN 0 COUNT 100"
qy exec cache --sql "HGETALL user:42"
```

Read-only rail applies here too: `GET/SCAN/TYPE/TTL/HGETALL` pass; `SET/DEL/FLUSHALL` are blocked without `--write`. In the GUI, redis keys are clickable with TYPE-aware value display.

## Groups & env-sets

Connections can be organized into **project folders** (`group`) and **env-sets** (same `db`, different `env`, shared schema):

```toml
[shop_dev]
url = "postgresql://…dev…/shop";  group = "shop"; db = "shop"; env = "dev"
[shop_prod]
url = "postgresql://…prod…/shop"; group = "shop"; db = "shop"; env = "prod"
```

- Connections with the same `db` fold into one env-set — one saved query runs against any environment: `qy exec shop --env prod`
- Unspecified env defaults to `dev` (the safest)
- The GUI shows an environment switcher (prod turns red)

## Multiple workspaces

`qy` aggregates all workspaces listed in `~/.config/quarry/config.toml` — one GUI/CLI over all your projects:

```bash
qy workspace add ~/projects/acme/db-workspace
qy workspace add ~/projects/side-project/db
qy connections    # both projects, grouped
qy gui            # sidebar shows both groups side by side
```

`--workspace a:b` (os.pathsep-separated) works as a temporary override; the first directory is primary for writes.

## GUI

![Quarry GUI](site/assets/gui-dark.png)

`qy gui` — a local, zero-build web GUI (Slate & Copper theme, light/dark):

- Grouped sidebar tree with env switcher (prod turns red), connection health dots
- **Multi-tab editor** — each tab remembers its SQL + connection, across restarts
- SQL highlighting + local autocomplete (keywords / tables / columns)
- **EXPLAIN button** — one click to the query plan
- Type-aware data grid: sorting, column resize, **keyboard navigation** (arrows + Enter), cell inspection with a **collapsible JSON tree**
- CSV/JSON export, **searchable query history** (with connection + time)
- TYPE-aware Redis key browsing

## Roadmap

- Column types in the result contract for all engines
- SQLite & DuckDB engines (zero-setup local demo)
- Redis key-namespace folding tree
- Cross-environment schema/data diff
- Write audit log (who ran what, where, when)
- Single-binary distribution

## Development

```bash
pip install -e ".[dev]"
createdb quarry_test && psql quarry_test -f tests/seed.sql
python3 -m pytest -q
```

DB-backed tests skip automatically when Postgres is unreachable; unit tests always run. See [CONTRIBUTING.md](CONTRIBUTING.md).

Quarry is developed and tested on macOS and Linux. Windows is currently untested (the psql/ssh integration and port takeover are Unix-flavored) — PRs welcome.

## License

[MIT](LICENSE)
