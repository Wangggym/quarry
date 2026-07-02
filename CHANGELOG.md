# Changelog

All notable changes to Quarry are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

## [0.2.0] — 2026-07-02

### Added
- **MCP face** (`qy mcp`): a Model Context Protocol server over stdio, pure
  stdlib. Six tools: `list_connections`, `list_tables`, `describe_table`,
  `exec_sql`, `list_saved_queries`, `run_saved_query`. Graduated write policy:
  server `--write` flag + per-call `write: true` + `confirm_prod: true` for prod.
- GUI: multi-tab editor (per-tab SQL + connection, persisted across restarts)
- GUI: EXPLAIN button (plan modal for postgres, grid for tabular plans)
- GUI: searchable query history with connection name and relative time
- GUI: grid keyboard navigation (arrow keys + Enter to inspect)
- GUI: collapsible JSON tree in the cell inspector

### Fixed
- SQL errors were silently swallowed into empty results (psql now runs with
  `ON_ERROR_STOP`; failed statements correctly exit with code 3)
- `EXPLAIN` / `SHOW` statements now work through `run_query` (previously broken
  by the JSON subquery wrapper) and are exempt from the auto-LIMIT injection

## [0.1.0] — 2026-07-02

First public release.

### Core
- Multi-engine query kernel (`quarry.core`): PostgreSQL (via system `psql`),
  MySQL (optional `pymysql`), Redis (via `redis-cli`)
- Structured result contract: `{columns, rows, rowCount, truncated, elapsedMs, engine, sql}`
- Safety rails in the kernel: read-only by default (`--write` to allow),
  automatic `LIMIT 500` row cap, graduated prod confirmation
- Stable exit-code contract: `0` ok / `2` connection / `3` SQL / `8` safety block
- Workspace-as-code: `connections.toml` + named queries (`queries/**/*.sql`
  with `-- @meta` headers); multi-workspace aggregation via
  `~/.config/quarry/config.toml`
- Connection groups and env-sets (same logical db across dev/prod/…,
  `--env` switch, dev default)
- SSH tunnels via system `ssh` (`ssh_*` connection fields)

### CLI (`qy`)
- `connections` (list/add/set/remove/test), `exec`, `run`, `save`, `list`,
  `describe`, `schema`, `validate`, `fingerprint`, `audit`, `remove`, `edit`,
  `workspace` (list/add/remove), `gui`
- Output formats: table / json / ndjson / csv

### GUI (`qy gui`)
- Local zero-build web GUI, light/dark theme
- Grouped sidebar tree with environment switcher (prod highlighted red)
- SQL editor with syntax highlighting and local autocomplete (keywords/tables/columns)
- Data grid: type-aware coloring, sorting, column resize, cell/row inspection
- CSV/JSON export, query history, saved-query library
- TYPE-aware Redis key browsing
- State persists across restarts (selected connection, SQL, results cache)
