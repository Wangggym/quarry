# Changelog

All notable changes to Quarry are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

### Local dev containers (`qy local`)

- **`qy local up [--engine postgres|redis|all]`** starts a local Postgres/Redis
  in a docker container on a fixed port (postgres `5433`, redis `6380`) with a
  named data volume, so a service running locally can talk only to `localhost`
  instead of a shared remote database. It's idempotent — repeat runs never spawn
  a duplicate container. The image tag is overridable with `--image`.
- **`qy local up <key>`** additionally auto-registers an `env=local` connection
  in `connections.toml` (one logical database per key inside the shared Postgres
  container) and joins it to the existing env-set, so `qy connections` shows the
  new `local` environment. Re-running never overwrites a local connection you've
  hand-edited.
- **`qy local down`** stops the container but keeps the data volume; **`down
  --purge`** also deletes the volume so the next `up` starts from an empty
  database.
- **`qy local status`** shows whether each container is running, its port, and
  its image, and points to `qy local up` when nothing is running.
- Readable errors when docker is missing, the daemon is down, or the port is
  already in use — no raw docker stack traces.

### GUI UX fixes

- **Hand-written SQL is never silently lost.** Clicking a table / redis key /
  saved query / history entry used to overwrite the editor; the draft is now
  pushed to History first, and Cmd/Ctrl+↓ at the end of a history walk restores
  the in-flight draft instead of clearing the editor.
- **Switching the env pill to `prod` no longer auto-runs the current SQL** — it
  shows a notice and waits for an explicit Run (non-prod env switches still
  re-run, as before).
- **Overlapping queries are latest-wins**: a slow, older response can no longer
  overwrite the result of a newer run/inspect after it painted.
- **Column sort is numeric-aware** (`'10'` sorts after `'9'` in text columns), a
  third click on the same column restores the original row order, and the sort
  arrow resets when a new result arrives.
- **Max-rows selector** in the toolbar (100/500/2000/5000, persisted) — results
  were previously hard-capped at 500 with no way to raise or lower the cap.
- **CSV export** now writes `quarry-<db>.csv` with a UTF-8 BOM (Excel no longer
  garbles non-ASCII); JSON export is named `quarry-<db>.json`.
- **Health dots carry the failure reason** as a row tooltip (was: a red dot with
  no explanation), and clicking an unreachable connection shows the error in the
  table panel.
- **"Copied" toast is honest** — it only shows after the clipboard write
  succeeds; failures show a copy-failed toast.
- **Network failures show a readable error** (was: `{}` from a stringified
  TypeError when the server was unreachable).
- **Generated table queries quote mixed-case/reserved identifiers** (postgres
  `"Name"`, mysql backticks) — clicking such a table no longer errors.
- **Redis key list cap is visible**: when the 400-key cap is hit the panel says
  "showing only the first N keys" instead of silently truncating.
- Escape now closes the **topmost** modal (was: the oldest); the table-filter
  text survives the background (SWR) list refresh; icon-only header controls
  carry `aria-label`s.
- **Editor tabs got real isolation.** Switching tabs now switches the result
  grid/status too, and CSV/JSON exports always contain the *active* tab's data
  (previously the grid kept showing another tab's result, and an export could
  write tab A's rows under tab B's filename). Closing a tab pushes its SQL to
  History — the "never silently lose SQL" invariant now covers all five editor
  overwrite sites. A tab whose connection no longer exists unbinds cleanly
  instead of silently rebinding to whatever was selected before.
- **Every tab's result now survives a reload**, not just the active tab's:
  switching to a background tab after reopening the page shows its last grid
  again (still isolated — a tab never shows another tab's data).
- **Saved queries persist under their own connection.** A saved query runs on
  the connection it declares (`@db`), not the tab's current one; when launched
  from a tab bound to a different connection its result is now tagged and
  persisted under the producing connection (and the tab re-pointed to it), so a
  reload restores it under the right connection instead of mislabeling it.
  (Consistency when the saved query's `@db` is a logical env-set is tracked
  separately in #18.)
- **Table list**: the currently open table is highlighted (cleared when custom
  SQL runs); a refresh button re-fetches the list on demand (tables and redis
  keys); Alt+click inserts the generated SQL without running it; lists that hit
  the 5000-table cap say so instead of silently truncating.

### Testing

- `TESTING.md` documents the **three-audit method** (existence / capability /
  shared-state) that keeps the feature matrix honest, plus a Design-gaps table
  for capabilities that are known-missing on purpose.
- New browser-e2e module `tests/test_gui_browser_features.py` (53 tests):
  env-set pills + prod guard, draft preservation, request-race, numeric sort,
  redis key tree + cap notice (auto-spawns an ephemeral `redis-server` when none
  is running), health-dot flows against a dead connection, SWR refresh, layout
  drags, export content (BOM/escaping), clipboard paths, persistence across
  reloads, grid keyboard nav, autocomplete columns, and more. Console errors are
  an autouse invariant in that module.
- Browser fixtures now stub the icon-font CDN with an empty local response, so
  the whole browser suite is hermetic (no external network) — this removed an
  intermittent `networkidle` timeout and cut the full-suite wall time in half.
- `TESTING.md` now carries a **GUI feature matrix** (66 rows) mapping every
  frontend feature point to its covering test; `AGENTS.md` documents the rule
  that keeps it current.

### Security / correctness fixes

- **Read-only rail could be bypassed** — now closed. `EXPLAIN SELECT 1; DROP TABLE t`
  previously passed the read-only check and executed the `DROP`; data-modifying CTEs
  (`WITH d AS (DELETE … RETURNING *) SELECT …`) slipped through the same way. The guard
  now rejects multiple statements and data-modifying CTEs across the CLI, GUI, and MCP
  faces (backed by a comment/string/dollar-quote-aware SQL skeleton).
- Auto-`LIMIT` no longer corrupts `FETCH FIRST … ROWS ONLY` or `… FOR UPDATE` queries,
  and `LIMIT` inside a string literal is no longer mistaken for a real limit.
- `qy run --limit/--full` no longer produces invalid SQL on nested/subquery `LIMIT`s
  (depth-aware, outer-only rewrite).
- `qy --max-rows N` now returns exactly N rows (was N+1).
- Redis read-only guard now blocks write-via-subclause commands (`SORT … STORE`,
  `GETEX`, `BITFIELD … SET`, `*STORE`, blocking pops, admin commands).
- `serialize_row` no longer crashes on a bare `date` (MySQL `DATE` / Neptune dates);
  `bytearray`/`memoryview` (pymysql BLOB/BINARY) now decode like `bytes`.
- Named-parameter substitution is single-pass — a value containing `:name` is no longer
  re-substituted.

### Fixed

- MySQL / Neptune driver failures now surface as clean errors with correct exit codes
  instead of raw tracebacks (CLI) or `-32603` protocol crashes (MCP).
- MCP `list_tables` no longer returns empty on MySQL 8 (case-insensitive `table_name`);
  malformed tool calls return a tool `isError` result, not a protocol crash.
- GUI: `/api/inspect` rejects non-Redis connections; missing/invalid request fields
  return clean `400`s instead of tracebacks; the health cache honors a 120s TTL, so a
  transient failure no longer pins a connection red forever; `_reclaim_port` never
  SIGTERMs a foreign process whose command merely ends in `gui`.
- `qy save` / `qy validate` resolve a logical env-set db like `qy run` does; `qy validate`
  refuses to validate a non-read-only saved query (validation stays side-effect-free);
  `qy exec/save --file <missing>` and MySQL/Neptune connection errors give clean messages.

### Changed (behavior — note when upgrading)

- The read-only rail is **stricter**: multiple statements and data-modifying CTEs are now
  rejected without `--write`. Scripts that relied on the previous (unsafe) pass-through
  will be blocked (exit `8`) — pass `--write` if the writes are intended.

### Added — tests & tooling

- Layered test suite (unit / integration / e2e / browser): **723 tests**, up from 69.
- Playwright headless-browser GUI e2e covering the real frontend (grid, run, EXPLAIN,
  export, tabs, theme/language, saved-query params, autocomplete, console-cleanliness).
- Coverage gate: unit + integration ≥ 95% (currently 99.6%) via `make cov`.
- `make test` (layered summary) / `make test-browser`; CI `coverage` + `browser` jobs
  (+ a Redis service); `TESTING.md` documents the architecture. See also `scripts/`.

### Internal

- repo-evolve parity E2E verification (2026-07-08): CHANGELOG-only change to
  validate the flywheel end-to-end in place of quarry-evolve parity phase B.

## [0.2.2] — 2026-07-02

- Fix MCP Registry name casing (`io.github.Wangggym/quarry`)

## [0.2.1] — 2026-07-02

- MCP Registry listing (`io.github.wangggym/quarry`) — README ownership marker
- Promo site polish: bilingual pages, hero showcase, nav fixes

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
