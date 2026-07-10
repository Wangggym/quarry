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
frontend (React + TypeScript, `web/src/`) is covered by the `browser` suite
(behaviorally), not by Python line coverage. Genuinely unreachable defensive
code (blocking `serve_forever`, `# unreachable` lines after `err(...)`) is
excluded via `[tool.coverage.report]` in `pyproject.toml`.

## CI

`.github/workflows/ci.yml` runs four kinds of jobs: the layered suite on the
boundary Python versions 3.11 and 3.13 (with Postgres + Redis services), a
`coverage` gate job on 3.12 that also runs the e2e layer (so no version×layer
combination runs twice), a `browser` job (headless Chromium, cached between
runs), and a package `build` check. Every job builds the web frontend (`web/`,
`npm ci && npm run build`) before Python tests so `/app` assets are present.

## GUI feature matrix

The GUI is a React + TypeScript SPA (`web/src/`) served under `/app` (the
default landing page — `/` redirects there) by the stdlib-only backend in
`src/quarry/gui.py` (`http.server` + `/api/*`). Because the feature surface
lives in a handful of components and stores, it can be enumerated
*exhaustively* — this matrix is that enumeration, and it is the source of
truth for frontend coverage. The backend (8 API endpoints, cache, health TTL,
port takeover, Host/Origin guard) is fully covered by `test_gui_backend.py` /
`test_gui_api.py` / `test_cov_gui.py` and is not repeated here.

### Keeping the matrix honest: three audits

The matrix stays trustworthy only if three *different* audits all close over
it — each catches a class of gap the others are structurally blind to. (The
blind spots are real: audit 1 alone shipped a matrix that said "tabs ✅" while
tab-switching silently kept — and exported — another tab's result set.)

1. **Existence audit** (code → matrix). Every interaction binding in
   `web/src/*.tsx` (click/keydown handlers, form submits), every
   `localStorage` key (`qy_react_*`, plus the legacy `/` GUI keys still read
   for one-time migration — see R65), and every `/api/*` endpoint the
   frontend fetches must map to a row. *Catches:* implemented-but-untested
   behavior. *Blind to:* features that should exist but don't, and
   cross-feature state bugs.
2. **Capability audit** (design → matrix). For each UI region (header, sidebar,
   tabs, editor, toolbar, grid), enumerate what a user of a SQL client would
   *expect* there (use DataGrip / TablePlus conventions as the reference), diff
   against the implementation, and file every miss in the **Design gaps** table
   below — scheduled or explicitly "won't do", never undocumented. *Catches:*
   missing features (this audit found the per-tab-result gap).
3. **Shared-state audit** (state × features). Store state in `web/src/store/`
   (`connStore`, `tabsStore` — including per-tab result snapshots, `uiStore`),
   plus component-local state (sort/selection, request-tracking guards). When
   a change writes any of these, check **every reader** before shipping; any
   two features sharing state need an interaction test. *Catches:*
   cross-feature bugs invisible to per-feature rows (tab switch updated the
   active connection while the grid still held another tab's rows → CSV
   exported wrong data under the new tab's filename).

One more rule from a real regression: when adding a UX invariant ("SQL is never
silently lost"), grep for **all** write sites of the protected state and cover
each — the invariant first landed on 4 of the 5 editor-overwrite sites; the 5th
(closing a tab) shipped unprotected.

Status: ✅ covered · 🟡 partial · ❌ uncovered. Tests live in
`tests/test_gui_react_app.py`.

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| R1 | global | `/app` placeholder mounts; shows Quarry + version from `/api/version` | test_gui_react_app:test_react_app_mounts_and_shows_version | ✅ |
| R2 | global | `/api/version` JSON endpoint | test_gui_react_app:test_api_version | ✅ |
| R3 | global | wheel includes `quarry/web_dist/` | test_gui_react_app:test_wheel_includes_web_dist, CI build job | ✅ |
| R4 | sidebar | sidebar table-structure browser: pick connection, list tables, show column name + type (issue #11) | test_gui_react_app:test_schema_browser_shows_table_columns_and_types | ✅ |
| R5 | sidebar | switching tables replaces the column list (no stale/merged columns) | test_gui_react_app:test_schema_browser_switching_tables_replaces_columns | ✅ |
| R6 | grid | SQL execution + result grid/status under `/app` (no legacy DOM dependency) | test_gui_react_app:test_react_result_grid_runs_sql_and_shows_status | ✅ |
| R7 | grid | numeric-aware sort + 3rd click restores original order | test_gui_react_app:test_react_grid_sort_third_click_restores_original_order | ✅ |
| R8 | grid | truncated results paginate via "load more" (offset-based) | test_gui_react_app:test_react_load_more_paginates_truncated_result | ✅ |
| R9 | grid | JSON cell modal + row-detail modal; Escape closes the topmost modal | test_gui_react_app:test_react_json_modal_and_row_detail, test_gui_react_app:test_react_grid_keyboard_nav_and_enter_opens_json_modal | ✅ |
| R10 | grid | CSV/JSON export from active grid result | test_gui_react_app:test_react_csv_json_export | ✅ |
| R11 | grid | cell type coloring (num/uuid/ts/bool/null) | test_gui_react_app:test_react_cell_type_coloring | ✅ |
| R12 | grid | column width drag | test_gui_react_app:test_react_column_width_drag | ✅ |
| R13 | grid | cell select; dblclick a short non-JSON value copies it (toast) | test_gui_react_app:test_react_cell_dblclick_copies_short_value | ✅ |
| R14 | grid | grid keyboard nav: arrows move selection, Enter opens the selected cell | test_gui_react_app:test_react_grid_keyboard_nav_and_enter_opens_json_modal | ✅ |
| R15 | grid | 0-row empty state | test_gui_react_app:test_react_zero_rows_empty_state | ✅ |
| R16 | grid | network/query error shows a readable message (not raw JSON) | test_gui_react_app:test_react_network_error_shows_readable_message | ✅ |
| R17 | sidebar | clicking a table generates a `limit 5` preview query, not `limit 100` (same cap as the legacy sidebar) | test_gui_react_app:test_react_table_click_generates_limit_5_preview | ✅ |
| R18 | editor | SQL editor: syntax-highlight overlay (keyword/string/comment) with scroll sync | test_gui_react_app:test_react_sql_highlight_overlay | ✅ |
| R19 | editor | editor placeholder reflects the active connection (SQL hint vs redis-command hint) | test_gui_react_app:test_react_placeholder_states | 🟡 |
| R20 | editor | Cmd/Ctrl+Enter runs the query from the editor | test_gui_react_app:test_react_ctrl_enter_runs_query | ✅ |
| R21 | editor | Cmd/Ctrl+↑/↓ walks SQL history without losing the in-progress draft | test_gui_react_app:test_react_history_nav_stashes_and_restores_draft | ✅ |
| R22 | editor | draft-preservation invariant: any editor overwrite (e.g. table click) stashes the hand-written draft into History, recoverable from the History panel | test_gui_react_app:test_react_table_click_preserves_draft_in_history | ✅ |
| R23 | editor | autocomplete: bare-word keyword suggestions, Tab accepts | test_gui_react_app:test_react_autocomplete_keyword | ✅ |
| R24 | editor | autocomplete: table names, narrowed to tables-only after FROM/JOIN/INTO/UPDATE; mouse-click accepts | test_gui_react_app:test_react_autocomplete_table_and_from_narrows | ✅ |
| R25 | editor | autocomplete: `table.column` suggestions fetched via `/api/columns`; Escape closes | test_gui_react_app:test_react_autocomplete_table_dot_column | ✅ |
| R26 | editor | editor height is drag-resizable and persists across reloads | test_gui_react_app:test_react_editor_height_drag_persists | ✅ |
| R27 | sidebar | sidebar: connection groups (workspace-origin label) collapse/expand and persist across reload | test_gui_react_app:test_react_sidebar_group_collapse_persists_across_reload | ✅ |
| R28 | sidebar | sidebar: health dots instant-paint from cache on load, "Check health" probes fresh and repaints ok/down | test_gui_react_app:test_react_health_dots_paint_from_cache_and_manual_check | ✅ |
| R29 | sidebar | sidebar: env pills switch connections; clicking a prod pill never auto-reruns the current query, non-prod pills do | test_gui_react_app:test_react_env_pill_prod_skips_autorun_nonprod_reruns | ✅ |
| R30 | sidebar | sidebar: redis key tree folds by `:`, expanded by default, shows type/TTL badges, narrows with the filter box, click-to-inspect | test_gui_react_app:test_react_redis_tree_badges_filter_and_inspect | ✅ |
| R31 | sidebar | sidebar: a capped redis key list shows a "first N keys" notice | test_gui_react_app:test_react_redis_capped_key_list_shows_notice | ✅ |
| R32 | sidebar | sidebar: saved queries run instantly when param-less; a param modal opens for parameterized ones, pre-filling defaults, Enter submits | test_gui_react_app:test_react_saved_queries_paramless_run_and_param_modal | ✅ |
| R32b | sidebar | running a saved query stashes a hand-written, never-run draft into History instead of discarding it | test_gui_react_app:test_react_saved_query_run_preserves_draft_in_history | ✅ |
| R33 | sidebar | sidebar: the saved-query param modal closes on click-out | test_gui_react_app:test_react_saved_query_modal_closes_on_clickout | ✅ |
| R34 | sidebar | sidebar width is drag-resizable and persists | test_gui_react_app:test_react_sidebar_width_drag_persists | ✅ |
| R35 | sidebar | sidebar: manual table/key refresh preserves the current filter text | test_gui_react_app:test_react_table_refresh_preserves_filter_text | ✅ |
| R36 | tabs | tab bar: add/switch/close tabs, each with its own SQL draft, tab count + active tab's SQL persist across reload; the sole remaining tab has no close (×) button (issue #50) | test_gui_react_app:test_react_tab_add_switch_close_and_persist | ✅ |
| R37 | tabs | tab title: defaults to `db@env`; double-click renames, Enter/blur commits, Escape reverts, an empty name reverts to the auto title, a custom title survives reload | test_gui_react_app:test_react_tab_title_shows_db_at_env_and_rename | ✅ |
| R38 | tabs | closing a tab (active or inactive) with an un-run draft stashes that SQL into History, never silently discarding it | test_gui_react_app:test_react_tab_close_preserves_sql_in_history | ✅ |
| R39 | tabs | tab bar: drag-and-drop reorders tabs; the active tab follows its id (not its old index), order persists across reload | test_gui_react_app:test_react_tab_drag_reorder_moves_active_tab | ✅ |
| R40 | tabs | middle-click closes a tab, same as the × glyph; a no-op when it is the only tab left | test_gui_react_app:test_react_tab_middle_click_closes | ✅ |
| R41 | tabs | Cmd/Ctrl+Shift+W closes the active tab; a no-op when it is the only tab left | test_gui_react_app:test_react_tab_keyboard_shortcut_closes_active_tab | ✅ |
| R42 | tabs | connection isolation: each tab's result grid is its own — a tab with no result of its own shows the empty placeholder, never a stale grid carried over from whichever tab was active before | test_gui_react_app:test_react_tab_switch_isolates_result_grid_between_tabs | ✅ |
| R43 | tabs | connection isolation: a request fired from tab A lands in tab A's own result slot even if the user has since switched to tab B — never repainted onto whichever tab happens to be active when the response arrives | test_gui_react_app:test_react_inflight_response_lands_on_origin_tab_not_newly_active_tab | ✅ |
| R44 | tabs | connection isolation: a result is tagged with its PRODUCING connection; rebinding the tab to another connection (env pill, no autorun) and reloading must not restore the old grid mislabeled as the new connection's | test_gui_react_app:test_react_result_not_restored_after_tab_rebound_to_different_connection | ✅ |
| R45 | tabs | connection isolation: an in-place connection switch (env pill) never touches the currently-painted grid while that tab stays active; leaving the tab and returning re-validates it against the connection current at that moment, same as a reload | test_gui_react_app:test_react_result_stays_until_tab_switch_then_clears_on_return_after_rebind | ✅ |
| R46 | tabs | connection isolation: a request in flight whose own tab is re-pointed to another connection before it resolves is dropped — never repainted, never persisted, as if it belonged to the new connection | test_gui_react_app:test_react_inflight_response_dropped_when_same_tab_switches_connection_mid_flight | ✅ |
| R47 | tabs | connection isolation: a saved query runs on its own connection; launched from a tab bound to a different one, the tab is re-pointed to the producing connection so the result is tagged/persisted/restored under it, not orphaned under the tab's launch-time connection | test_gui_react_app:test_react_saved_query_result_persisted_under_producing_connection | ✅ |
| R48 | tabs | connection isolation: the R47 tagging contract also holds when the saved query's `@db` is a LOGICAL env-set name (not a concrete connection key) — resolved via `resolve_connection`'s env-set lookup branch, the launching tab is still re-pointed to the connection the query actually ran on | test_gui_react_app:test_react_saved_query_with_logical_envset_db_retargets_tab | ✅ |
| R49 | tabs | connection isolation: "Load more" pagination is hidden once the tab's current connection has drifted from the one that produced the shown (truncated) page — an in-place rebind must not let a later page get fetched from a connection the tab no longer points at | test_gui_react_app:test_react_load_more_disabled_after_inplace_connection_rebind | ✅ |
| R50 | tabs | connection isolation: a request's failure is tagged and persisted per-tab exactly like a success — a query that errors while its tab is in the background is not silently dropped, it surfaces once the user returns to that tab | test_gui_react_app:test_react_background_tab_error_surfaces_when_returned_to | ✅ |
| R51 | header | header: brand + workspace label (multi-workspace count + tooltip), read-only badge | test_gui_react_app:test_react_header_shows_workspace_label_and_readonly_badge | ✅ |
| R52 | header | header: prod badge shows only on a prod-env connection | test_gui_react_app:test_react_header_prod_badge_shows_for_prod_env_only | ✅ |
| R53 | header | header: language toggle (中/EN) flips all chrome strings and persists across reload | test_gui_react_app:test_react_header_language_toggle_persists | ✅ |
| R54 | header | header: theme toggle (light/dark) flips `data-theme` and persists across reload | test_gui_react_app:test_react_header_theme_toggle_persists | ✅ |
| R55 | header | connection-info modal: resolved URL defaults masked, live reachability probe; click-outside closes | test_gui_react_app:test_react_conninfo_modal_shows_masked_url_and_health | ✅ |
| R56 | header | connection-info modal: eye toggles masked↔revealed, copy always puts the real URL on the clipboard | test_gui_react_app:test_react_conninfo_reveal_and_copy_real_url | ✅ |
| R57 | header | connection-info modal: "Create local env" offered only when the env-set has no local member | test_gui_react_app:test_react_conninfo_offers_create_local_when_set_has_none | ✅ |
| R58 | header | connection-info modal: "Sync schema from {env}" offered only on the local env | test_gui_react_app:test_react_conninfo_offers_sync_on_local_env | ✅ |
| R59 | header | workspace-manager modal: list registered workspaces (flags missing dir), add, remove (confirm-gated), click-outside closes | test_gui_react_app:test_react_workspace_manager_add_flags_missing_and_remove | ✅ |
| R59b | header | workspace add/remove refreshes the sidebar/header connection set immediately, without a page reload; removing the workspace behind the currently selected connection unbinds it right away (never silently rebinds to another one) | test_gui_react_app:test_react_workspace_manager_add_and_remove_refreshes_connections_live, test_gui_react_app:test_react_workspace_manager_remove_unbinds_active_connection_immediately | ✅ |
| R60 | toolbar | toolbar: Format button uppercases keywords and inserts newlines before clauses | test_gui_react_app:test_react_format_button_uppercases_and_newlines | ✅ |
| R61 | toolbar | toolbar: EXPLAIN opens a single-column plan modal; Escape closes it | test_gui_react_app:test_react_explain_opens_plan_modal_and_escape_closes | ✅ |
| R62 | toolbar | toolbar: EXPLAIN guards — redis toast, suppressed if its tab is switched/re-pointed mid-flight | test_gui_react_app:test_react_explain_redis_toast, test_gui_react_app:test_react_explain_suppressed_when_tab_switched_mid_flight | 🟡 |
| R63 | toolbar | toolbar: History modal — empty state, search filters entries, relative-time display, recall into editor closes the modal | test_gui_react_app:test_react_history_modal_empty_state, test_gui_react_app:test_react_history_modal_search_filters_and_shows_relative_time | ✅ |
| R64 | toolbar | toolbar: max-rows selector persists across reload | test_gui_react_app:test_react_max_rows_selector_persists_across_reload | ✅ |
| R65 | global | localStorage consolidation (issue #53): every legacy `/` GUI key (`qy_lang qy_theme qy_sw qy_edh qy_maxrows qy_collapsed qy_hist qy_tabs qy_ati qy_ui qy_tabres qy_result`) has a one-time migration path into the React store's own `qy_react_*` keys, converged (written back) the first time the latter has never been written — including the db+env-validated upgrade of the two legacy result formats (`qy_tabres`, `qy_result`) into per-tab results, an ungrouped connection group's localized `::other`/`::其他` collapse key normalized to React's own `${ws}::` format, and legacy `qy_hist`'s even-older bare-string entries normalized into `{sql,db,env,ts}` | test_gui_react_app:test_react_legacy_scalar_prefs_migrate_on_first_load, test_gui_react_app:test_react_legacy_collapsed_groups_migrate_on_first_load, test_gui_react_app:test_react_legacy_collapsed_ungrouped_key_migrates_on_first_load, test_gui_react_app:test_react_legacy_history_migrates_on_first_load, test_gui_react_app:test_react_legacy_history_bare_string_entries_migrate, test_gui_react_app:test_react_legacy_qy_ui_migrates_into_tabs, test_gui_react_app:test_react_legacy_qy_tabs_migrates_on_first_load, test_gui_react_app:test_react_legacy_qy_tabres_migrates_on_first_load, test_gui_react_app:test_react_legacy_qy_result_env_mismatch_not_restored, test_gui_react_app:test_react_legacy_qy_result_env_match_restored | ✅ |

🟡 R19: the "pick a connection" placeholder (no `db` selected yet) is not
independently browser-tested — a connection is always auto-selected as soon as
one exists, so that state is only reachable with zero configured connections,
which the schema panel already short-circuits before the editor renders.

🟡 R42-R50 (issue #51): every connection-isolation point #18 lists that's
reachable in the GUI today is covered above, including the
logical-env-set saved-query case (R48), and now EXPLAIN's own in-flight
suppression (R62), which reuses the same per-tab request-tracking machinery.

🟡 R62: the EXPLAIN button is disabled whenever no connection is selected, so
the "no connection" toast branch in `runExplain` is unreachable from the UI
and untested (a defense-in-depth dead guard, kept deliberately). The
multi-column-falls-through-to-grid path and the disabled-while-running state
are also not independently asserted, since exercising a genuinely
multi-column EXPLAIN plan needs a MySQL connection not available in this
test environment.

Full state restore after a reload (conn + sql + result + widths + collapse)
has no single combined row — it is spread across per-feature rows instead:
R26 (editor height), R27 (group collapse), R34 (sidebar width), R36 (tabs +
active SQL), R53/R54 (lang/theme), R64 (max-rows), and R44 (result, tagged
to its producing connection) all independently persist across a reload,
backed by the unified `uiStore`/`tabsStore` (R65) rather than ad hoc
per-component `localStorage` calls.

### Design gaps (capability-audit output — missing on purpose until scheduled)

| Region | Missing capability | Decision |
|--------|--------------------|----------|
| sidebar | row-count / size hints next to tables | backlog (low) |
| header | icon-only buttons (⚙ ⓘ lang/theme) carry a `title` tooltip but no `aria-label` | backlog (low) |

Safety-relevant UX invariants that must never regress (R22/R38 draft
preservation, R29 prod auto-run guard, R43/R46 stale-response handling, R7
numeric sort): draft SQL is never silently lost; switching to prod never
auto-runs; stale responses never overwrite newer results; sort is
numeric-aware and restorable.

## Adding tests

- Put pure logic in a `unit` test (no fixtures that touch a DB/server).
- Use `ws`/`pg_exec` for in-process DB work, `gui_server` for the GUI HTTP API,
  `qy`/`mcp` for CLI/MCP subprocess e2e, and `page` for browser e2e.
- Never mutate the shared `customers`/`orders` seed tables; create scratch tables
  with a per-file prefix and drop them in the test.
