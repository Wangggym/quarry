# Testing

Quarry has a layered test suite so failures are easy to localize and the whole
thing stays green on a laptop with nothing installed (engine-dependent tests skip
rather than fail; CI provides the engines and runs everything).

## Layers

| Marker        | What it covers                                              | Needs |
|---------------|-------------------------------------------------------------|-------|
| `unit`        | pure logic + mocked externals (safety rails, SQL skeleton, param substitution, redis guard, formatters, URL parsing, cache, port logic) | nothing |
| `integration` | in-process against a real database, incl. the in-thread GUI HTTP server and in-process CLI/MCP dispatch | Postgres |
| `e2e`         | a real external process: the `qy` CLI and `qy mcp` stdio server as subprocesses | Postgres |
| `browser`     | the real GUI frontend driven in headless Chromium (Playwright) | Postgres + Playwright |

`browser` is a sub-kind of `e2e` (a test can carry both markers). Every test is
auto-assigned a layer from the fixtures it uses, so `pytest -m unit` etc. always
partition the whole suite.

## Running

```bash
make test              # unit → integration → e2e, with a per-layer PASS/FAIL summary
make test-unit         # one layer (also: test-integration, test-e2e, test-browser)
make cov               # coverage gate: unit + integration must be ≥ 95%
make test-browser      # headless-browser GUI suite (needs the browser installed once)
```

Under the hood these call `scripts/test.sh` (layered runner) and
`scripts/coverage.sh` (the gate). Plain `pytest` also works and prints an
environment header showing which engines were detected.

## One-time setup

```bash
make install                                   # pip install -e ".[dev]"
make seed                                       # create + seed the quarry_test database
# for the browser suite:
pip install -e ".[e2e]" && make browser-install # installs Playwright + headless Chromium
```

Override the database URL with `QUARRY_TEST_DB_URL` (defaults to
`postgresql://localhost:5432/quarry_test`). MySQL tests run only when
`QUARRY_TEST_MYSQL_URL` is set; Redis and MySQL execution paths are otherwise
covered by mocked tests so they run everywhere.

## Coverage

The gate (`make cov`) measures **unit + integration** coverage of the `quarry`
package and fails under 95%. It deliberately excludes the `e2e`/`browser` layers
so the number reflects what the fast in-process suites alone exercise. The
frontend JavaScript lives inside the `INDEX_HTML` string literal and is covered
by the `browser` suite (behaviorally), not by Python line coverage. Genuinely
unreachable defensive code (blocking `serve_forever`, `# unreachable` lines after
`err(...)`) is excluded via `[tool.coverage.report]` in `pyproject.toml`.

## CI

`.github/workflows/ci.yml` runs four jobs: the layered suite across Python
3.11–3.13 (with Postgres + Redis services), a `coverage` gate job, a `browser`
job (installs headless Chromium), and a package `build` check.

## Adding tests

- Put pure logic in a `unit` test (no fixtures that touch a DB/server).
- Use `ws`/`pg_exec` for in-process DB work, `gui_server` for the GUI HTTP API,
  `qy`/`mcp` for CLI/MCP subprocess e2e, and `page` for browser e2e.
- Never mutate the shared `customers`/`orders` seed tables; create scratch tables
  with a per-file prefix and drop them in the test.
