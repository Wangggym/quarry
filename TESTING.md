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

## GUI feature matrix

The whole GUI lives in one file (`src/quarry/gui.py`: ~440 lines of Python +
~800 lines of JS inside `INDEX_HTML`), so its feature points can be enumerated
*exhaustively* — this matrix is that enumeration, and it is the source of truth
for frontend coverage. The backend (7 API endpoints, cache, health TTL, port
takeover, Host/Origin guard) is fully covered by `test_gui_backend.py` /
`test_gui_api.py` / `test_cov_gui.py` and is not repeated here.

### Keeping the matrix honest: three audits

The matrix stays trustworthy only if three *different* audits all close over
it — each catches a class of gap the others are structurally blind to. (The
blind spots are real: audit 1 alone shipped a matrix that said "tabs ✅" while
tab-switching silently kept — and exported — another tab's result set.)

1. **Existence audit** (code → matrix). Every interaction binding in the JS
   (`grep 'onclick\|addEventListener\|oninput\|onmousedown'`), every
   `localStorage` key (`qy_lang qy_theme qy_sw qy_edh qy_tabs qy_ati qy_ui
   qy_maxrows qy_collapsed qy_hist qy_result`), and every `/api/*` endpoint the
   frontend fetches must map to a row. *Catches:* implemented-but-untested
   behavior. *Blind to:* features that should exist but don't, and
   cross-feature state bugs.
2. **Capability audit** (design → matrix). For each UI region (header, sidebar,
   tabs, editor, toolbar, grid), enumerate what a user of a SQL client would
   *expect* there (use DataGrip / TablePlus conventions as the reference), diff
   against the implementation, and file every miss in the **Design gaps** table
   below — scheduled or explicitly "won't do", never undocumented. *Catches:*
   missing features (this audit found the per-tab-result gap).
3. **Shared-state audit** (state × features). Global mutable JS state:
   `cur{db,env,engine,isRedis,table}`, `lastRes`, `TABS/ATI/TABRES`,
   `HIST/hi/draftStash`, `sortState`, `selTd`, `TCACHE`, `COLS`, `HEALTH`,
   `runSeq`, plus the localStorage keys above. When a change writes any of
   these, check **every reader** before shipping; any two features sharing
   state need an interaction test. *Catches:* cross-feature bugs invisible to
   per-feature rows (tab switch updated `cur.db` while `lastRes` still held the
   other tab's rows → CSV exported wrong data under the new tab's filename).

One more rule from a real regression: when adding a UX invariant ("SQL is never
silently lost"), grep for **all** write sites of the protected state and cover
each — the invariant first landed on 4 of the 5 editor-overwrite sites; the 5th
(closing a tab) shipped unprotected.

Status: ✅ covered · 🟡 partial · ❌ uncovered. Tests live in
`tests/test_gui_browser.py` (B) and `tests/test_gui_browser_features.py` (F).

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| 1 | header | brand + workspace label; multi-workspace count + tooltip | B:test_load_shows_brand_and_readonly_badge (single-ws only) | 🟡 |
| 2 | header | read-only badge | B:test_load_shows_brand_and_readonly_badge | ✅ |
| 3 | header | prod badge visibility | F:test_env_pills_default_dev_and_prod_badge | ✅ |
| 4 | header | health-check button: probe all, dots update, error tooltip | F:test_health_button_paints_ok_and_down_dots | ✅ |
| 5 | header | language toggle 中/EN (reload, full chrome, persistence) | B:test_language_toggle_switches_run_label, F:test_language_toggle_full_chrome | ✅ |
| 6 | header | theme toggle + persistence | B:test_theme_toggle_flips_data_theme, F:test_theme_persists_after_reload | ✅ |
| 7 | sidebar | connection groups + workspace origin label | B:test_load_shows_brand_and_readonly_badge (row presence only) | 🟡 |
| 8 | sidebar | group collapse/expand + persistence | F:test_group_collapse_persists_after_reload | ✅ |
| 9 | sidebar | health dot states (ok/down; dimmed row; error tooltip) | F:test_health_button_paints_ok_and_down_dots | ✅ |
| 10 | sidebar | instant dot paint from backend cache (`cached=1`) | F:test_health_dots_repaint_from_cache_after_reload | ✅ |
| 11 | sidebar | env pills render (default dev; prod styling) | F:test_env_pills_default_dev_and_prod_badge | ✅ |
| 12 | sidebar | pill click switches env; pill + header switcher sync | F:test_prod_env_switch_does_not_autorun, F:test_nonprod_env_switch_autoruns | ✅ |
| 13 | sidebar | env-switch auto-rerun — **never on prod** (toast instead) | F:test_prod_env_switch_does_not_autorun, F:test_nonprod_env_switch_autoruns | ✅ |
| 14 | sidebar | row click selects + opens table panel; re-click toggles panel | F:test_reclick_connection_toggles_panel | ✅ |
| 15 | sidebar | table filter box; filter survives the SWR repaint | F:test_table_filter_box (repaint-survival unasserted) | 🟡 |
| 16 | sidebar | table panel connection-error state / `no tables` empty state | F:test_dead_connection_click_shows_error_panel (error only) | 🟡 |
| 17 | sidebar | TCACHE instant paint + SWR background refresh | F:test_swr_refreshes_stale_table_list | ✅ |
| 18 | sidebar | redis key tree: `:` hierarchy, fold, count badges | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 19 | sidebar | redis type + TTL badges | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 20 | sidebar | redis key filter; key click → inspect grid | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 21 | sidebar | saved-query list (param badge, desc tooltip); paramless runs on click | F:test_saved_query_without_params_runs_directly | ✅ |
| 22 | sidebar | saved-query param modal (required/default, Enter submits, click-out closes) | B:test_saved_query_param_modal…, F:test_param_modal_enter_submits_and_clickout_closes | ✅ |
| 23 | sidebar | sidebar width drag + persistence | F:test_sidebar_width_drag_persists | ✅ |
| 24 | editor | SQL highlight overlay + scroll sync | F:test_sql_highlight_overlay (scroll sync unasserted) | 🟡 |
| 25 | editor | placeholder states (no conn / sql / redis) | F:test_placeholder_states, F:test_redis_key_tree… (redis) | ✅ |
| 26 | editor | Cmd/Ctrl+Enter runs | B:test_custom_sql_via_run_keyboard | ✅ |
| 27 | editor | Cmd/Ctrl+↑↓ history walk; draft stashed and restored at the bottom | F:test_history_nav_stashes_and_restores_draft | ✅ |
| 28 | editor | draft pushed to History before any overwrite (table/key/saved/recall) | F:test_table_click_preserves_draft_in_history | ✅ |
| 29 | editor | autocomplete: keywords + tables | B:test_autocomplete_keyword_and_table | ✅ |
| 30 | editor | autocomplete: `table.column` via /api/columns | F:test_autocomplete_columns_after_table_dot | ✅ |
| 31 | editor | autocomplete: FROM/JOIN table-only filter; dedup; 12-item cap | F:test_autocomplete_from_narrows_to_tables (dedup/cap unasserted) | 🟡 |
| 32 | editor | autocomplete keyboard nav (↑↓ Tab Enter Esc) + mouse pick | B/F autocomplete tests (partial) | 🟡 |
| 33 | editor | editor height drag + persistence | F:test_editor_height_drag_persists | ✅ |
| 34 | editor | tabs: add/switch/close/persist | B:test_tabs_add_switch_restore_and_close | ✅ |
| 35 | editor | tab title rule; legacy `qy_ui` migration | F:test_tab_title_shows_db_at_env, F:test_legacy_qy_ui_migrates_into_tabs | ✅ |
| 36 | toolbar | Run button + loading spinner | B:test_custom_sql_via_run_button, F:test_run_shows_loading_spinner | ✅ |
| 37 | toolbar | overlapping runs are latest-wins (stale response discarded) | F:test_stale_slow_response_does_not_overwrite | ✅ |
| 38 | toolbar | Format (uppercase + newlines) | B:test_format_button_uppercases_and_newlines | ✅ |
| 39 | toolbar | EXPLAIN: single-column plan modal + Esc closes | B:test_explain_opens_plan_modal_and_escape_closes | ✅ |
| 40 | toolbar | EXPLAIN guards: no-conn toast / redis toast / multi-col grid / disabled while running | F:test_explain_without_connection_toasts, F:test_redis_key_tree… (multi-col + disabled unasserted) | 🟡 |
| 41 | toolbar | CSV export: `quarry-<db>.csv`, UTF-8 BOM, quoting/escaping | F:test_csv_export_content_bom_and_escaping | ✅ |
| 42 | toolbar | JSON export: `quarry-<db>.json`, row content | F:test_json_export_content | ✅ |
| 43 | toolbar | history modal: list + recall into editor; Esc closes | B:test_history_lists_runs…, F:test_history_modal_escape_closes | ✅ |
| 44 | toolbar | history search / ago timestamps / empty toast / 100 cap | F:test_history_search_filters, F:test_history_empty_toast (ago + cap unasserted) | 🟡 |
| 45 | grid | render: sticky rownum, typed headers, zebra rows | B:test_click_table_renders_grid_with_types_and_status | ✅ |
| 46 | grid | cell type coloring (num/uuid/ts/bool/json/null) | F:test_cell_type_coloring, F:test_cell_json_opens_tree_modal | ✅ |
| 47 | grid | sort asc/desc, numeric-aware; 3rd click restores original order; arrow | B:test_sort_column_toggles_arrow_and_reorders, F:test_sort_numeric_strings_and_third_click_restores | ✅ |
| 48 | grid | sort state resets on a new result | F:test_new_result_resets_sort_state | ✅ |
| 49 | grid | column width drag | F:test_column_width_drag | ✅ |
| 50 | grid | cell select; dblclick long→modal / short→copy (honest toast) | B:test_cell_doubleclick_no_error, F:test_cell_copy_via_keyboard_and_dblclick | ✅ |
| 51 | grid | JSON tree modal + copy | F:test_cell_json_opens_tree_modal | ✅ |
| 52 | grid | row-detail modal (rownum click) | B:test_rownum_click_opens_row_detail_modal | ✅ |
| 53 | grid | keyboard nav: arrows move selection, Enter opens, Cmd+C copies | F:test_grid_keyboard_nav_and_enter_opens_modal, F:test_cell_copy_via_keyboard_and_dblclick | ✅ |
| 54 | grid | status bar: rows / elapsed / truncated / target | B:test_click_table…, F:test_truncated_badge_shows | ✅ |
| 55 | grid | 0-row empty state | F:test_zero_rows_empty_state | ✅ |
| 56 | grid | error pane (`.err`); network failures show a readable message | B:test_write_is_blocked…, F:test_network_error_shows_readable_message | ✅ |
| 57 | grid | result persisted to localStorage; restored after reload | F:test_editor_and_result_restored_after_reload | ✅ |
| 58 | grid | Escape closes the topmost modal | B:test_explain…, F:test_cell_json…, F:test_history_modal_escape_closes | ✅ |
| 59 | global | toast styles + durations (ok vs error) | F:test_cell_copy_via_keyboard_and_dblclick (ok style) + error-toast presence in guards | ✅ |
| 60 | global | read-only rail end-to-end | B:test_write_is_blocked_with_readonly_error | ✅ |
| 61 | global | full state restore after reload (conn + sql + result + widths + collapse) | F:test_editor_and_result_restored…, F:test_group_collapse…, F:test_sidebar_width…, F:test_editor_height… | ✅ |
| 62 | global | zero console errors as an invariant | F autouse `_console_clean`; B:test_no_console_errors_after_normal_flow | 🟡 |
| 63 | sidebar | redis key list cap notice ("showing first N keys") | F:test_redis_capped_key_list_shows_notice | ✅ |
| 64 | sidebar | generated table SQL quotes mixed-case/reserved identifiers | F:test_mixed_case_table_click_is_quoted | ✅ |
| 65 | toolbar | max-rows selector: caps results, persisted across reloads | F:test_max_rows_selector_caps_and_persists | ✅ |
| 66 | header | icon-only controls carry aria-labels | — (set in the i18n block; no axe pass yet) | 🟡 |
| 67 | tabs | per-tab result isolation: grid / status / export always reflect the active tab | F:test_tab_switch_isolates_results | ✅ |
| 68 | tabs | closing a tab pushes its SQL to History (active + inactive close) | F:test_close_tab_preserves_sql_in_history | ✅ |
| 69 | tabs | tab with a vanished connection unbinds (never silently rebinds) | F:test_stale_tab_connection_unbinds_not_rebinds | ✅ |
| 70 | sidebar | current-table highlight; cleared when custom SQL runs | F:test_table_click_highlights_current_table | ✅ |
| 71 | sidebar | manual list refresh button (tables + redis keys); filter survives refresh | F:test_table_list_manual_refresh (redis button + filter-survival unasserted) | 🟡 |
| 72 | sidebar | table list cap notice at 5000 | backend: test_api_tables_capped_flag_at_5000 (UI note unasserted) | 🟡 |
| 73 | sidebar | Alt+click inserts generated SQL without running | F:test_alt_click_inserts_without_running | ✅ |

### Design gaps (capability-audit output — missing on purpose until scheduled)

| Region | Missing capability | Decision |
|--------|--------------------|----------|
| tabs | rename / drag-reorder / middle-click close / Cmd+W-style shortcuts | backlog (low) |
| tabs | per-tab result persistence across reloads (only the active tab's result is restored) | backlog (low) |
| sidebar | table structure browser (columns/types in the UI; `/api/columns` only feeds autocomplete) | backlog (medium) |
| sidebar | row-count / size hints next to tables | backlog (low) |
| grid | true pagination / load-more (max-rows selector only raises the cap) | backlog (medium) |
| header | vendored icons — jsdelivr CDN dependency (row 66) | open design decision |
| header | multi-workspace management UI (list is display-only) | backlog (low) |

Safety-relevant UX invariants that must never regress (rows 13, 27–28, 37, 47):
draft SQL is never silently lost; switching to prod never auto-runs; stale
responses never overwrite newer results; sort is numeric-aware and restorable.

Known deliberate gap: the Tabler icon webfont still loads from the jsdelivr CDN
(offline → icon-only buttons render blank but stay clickable via tooltips).
Vendoring the ~22 glyphs (embedded font vs inline SVG) is a design decision left
open — see the matrix row 66 note.

## Adding tests

- Put pure logic in a `unit` test (no fixtures that touch a DB/server).
- Use `ws`/`pg_exec` for in-process DB work, `gui_server` for the GUI HTTP API,
  `qy`/`mcp` for CLI/MCP subprocess e2e, and `page` for browser e2e.
- Never mutate the shared `customers`/`orders` seed tables; create scratch tables
  with a per-file prefix and drop them in the test.
