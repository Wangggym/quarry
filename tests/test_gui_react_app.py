"""Browser + API checks for the React scaffold at /app (issue #21).

The legacy INDEX_HTML GUI at / is unchanged; this file only covers the new
strangler-fig shell served from web_dist.
"""

from __future__ import annotations

import re
import urllib.request
import zipfile
import json
from pathlib import Path

import pytest

from conftest import REPO, TEST_DB_URL, _running_gui, requires_browser
from quarry import __version__
from test_gui_browser_features import DEAD_TOML, _rcli, _redis_running

pytestmark = [requires_browser, pytest.mark.browser]


def _open_react_page(browser, base, viewport=None):
    ctx = browser.new_context(viewport=viewport or {"width": 1200, "height": 800})
    page = ctx.new_page()
    page.goto(f"{base}/app/", wait_until="networkidle")
    page.wait_for_selector("#react-sql-input", state="visible")
    return ctx, page


def _run_react_sql(page, sql):
    page.fill("#react-sql-input", sql)
    page.locator("#react-run-btn").click()
    page.wait_for_selector("#react-status", timeout=15000)


def test_react_app_mounts_and_shows_version(_pw_browser, tmp_path):
    """Placeholder React page loads at /app/ and reads version from /api/version."""
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 800, "height": 600})
        page = ctx.new_page()
        try:
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector("#root h1", state="visible")
            assert page.locator("#root h1").inner_text() == "Quarry"
            page.wait_for_selector("#root .version", state="visible")
            assert re.match(r"^v\d+\.\d+\.\d+$", page.locator("#root .version").inner_text())
        finally:
            ctx.close()


def test_schema_browser_shows_table_columns_and_types(_pw_browser, tmp_path):
    """Sidebar table-structure browser (issue #11): pick a table, see columns/types."""
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1000, "height": 700})
        page = ctx.new_page()
        try:
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"]', state="visible")
            assert page.locator(".run-target").inner_text() == "testpg@test"

            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')
            page.click('[data-testid="schema-tables"] button:has-text("customers")')

            rows = page.locator(".schema-columns-table tbody tr")
            page.wait_for_selector(".schema-columns-table")
            cells = {
                rows.nth(i).locator("td").nth(0).inner_text():
                    rows.nth(i).locator("td").nth(1).inner_text()
                for i in range(rows.count())
            }
            assert cells["id"] == "integer"
            assert cells["email"] == "text"
            assert cells["created_at"] == "timestamp with time zone"
        finally:
            ctx.close()


def test_schema_browser_switching_tables_replaces_columns(_pw_browser, tmp_path):
    """Selecting a second table must show ITS columns, not a stale/merged mix
    (the same cross-feature stale-state class of bug called out for tabs/#18)."""
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1000, "height": 700})
        page = ctx.new_page()
        try:
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')
            page.click('[data-testid="schema-tables"] button:has-text("customers")')
            page.wait_for_selector(".schema-columns-table")
            assert "email" in page.locator(".schema-columns-table").inner_text()

            page.click('[data-testid="schema-tables"] button:has-text("orders")')
            # the customers table (still mounted) matches ".schema-columns-table"
            # immediately, before React clears it to null and repaints with
            # orders' columns — a plain wait_for_selector()+inner_text() can win
            # that race and read the stale customers text, so poll for content.
            page.wait_for_function(
                "document.querySelector('.schema-columns-table')"
                "?.innerText.includes('amount')")
            text = page.locator(".schema-columns-table").inner_text()
            assert "amount" in text and "status" in text
            assert "email" not in text
        finally:
            ctx.close()


def test_schema_browser_shows_columns_for_quoted_special_char_table_name(
    _pw_browser, tmp_path, pg_exec,
):
    """A table name needing quoting (dash + space) must still show its real
    columns — regression: a character-stripping sanitizer used to match it
    against a mangled, non-existent identifier and render an empty schema
    for a table `/api/tables` had just listed as real."""
    pg_exec('DROP TABLE IF EXISTS "qy-review weird"')
    rc, _, err = pg_exec('CREATE TABLE "qy-review weird" (id serial PRIMARY KEY, note text)')
    assert rc == 0, err
    try:
        with _running_gui(tmp_path) as base:
            ctx = _pw_browser.new_context(viewport={"width": 1000, "height": 700})
            page = ctx.new_page()
            try:
                page.goto(f"{base}/app/", wait_until="networkidle")
                sel = '[data-testid="schema-tables"] button:has-text("qy-review weird")'
                page.wait_for_selector(sel)
                page.click(sel)
                page.wait_for_selector(".schema-columns-table")
                text = page.locator(".schema-columns-table").inner_text()
                assert "note" in text
                assert "no columns" not in page.locator('[data-testid="schema-columns"]').inner_text()
            finally:
                ctx.close()
    finally:
        pg_exec('DROP TABLE IF EXISTS "qy-review weird"')


_DELAY_COLUMNS_FOR_CUSTOMERS_INIT_SCRIPT = """
(() => {
    const origFetch = window.fetch;
    window.fetch = (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/api/columns") && url.includes("table=customers")) {
            return new Promise((resolve) => {
                setTimeout(() => resolve(origFetch(input, init)), 600);
            });
        }
        return origFetch(input, init);
    };
})();
"""


def test_schema_browser_stale_columns_response_does_not_overwrite(_pw_browser, tmp_path):
    """A slow /api/columns response for a table the user has already clicked
    AWAY from must never repaint the panel — latest-wins, same invariant class
    as the grid's runSeq guard in the legacy app.

    The delay is injected as an in-page `fetch` override (via an init script)
    rather than intercepted on the Python side: a Python route handler that
    sleeps can serialize with other concurrently-intercepted requests on the
    driver's dispatch thread, which defeats the very race this test needs to
    create between the slow "customers" response and the fast "orders" one.
    """
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1000, "height": 700})
        page = ctx.new_page()
        try:
            page.add_init_script(_DELAY_COLUMNS_FOR_CUSTOMERS_INIT_SCRIPT)
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')
            page.click('[data-testid="schema-tables"] button:has-text("customers")')  # slow, in-flight
            page.click('[data-testid="schema-tables"] button:has-text("orders")')      # fast, resolves first
            page.wait_for_selector(".schema-columns-table")
            text = page.locator(".schema-columns-table").inner_text()
            assert "amount" in text and "status" in text

            page.wait_for_timeout(800)                     # let the stale customers response land
            text_after = page.locator(".schema-columns-table").inner_text()
            assert "email" not in text_after                # must not have been overwritten
            assert "amount" in text_after and "status" in text_after
        finally:
            ctx.close()


def test_schema_browser_stale_tables_response_does_not_overwrite(_pw_browser, tmp_path):
    """A slow /api/tables response for a connection the user has already
    switched AWAY from must never repaint the table list — same latest-wins
    invariant as above, on the connection axis rather than the table axis.

    Same in-page fetch-override technique as the columns test above, for the
    same reason: a Python-side route delay would risk masking the race.
    """
    extra = f'\n[testpg2]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n'
    delay_tables_for_testpg2 = """
    (() => {
        const origFetch = window.fetch;
        window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            if (url.includes("/api/tables") && url.includes("db=testpg2")) {
                return new Promise((resolve) => {
                    setTimeout(() => {
                        resolve(new Response(JSON.stringify(
                            {engine: "postgres", tables: ["synthetic_stale_table"], capped: false}
                        ), {status: 200, headers: {"Content-Type": "application/json"}}));
                    }, 600);
                });
            }
            return origFetch(input, init);
        };
    })();
    """
    with _running_gui(tmp_path, extra_conn=extra) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1000, "height": 700})
        page = ctx.new_page()
        try:
            page.add_init_script(delay_tables_for_testpg2)
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"]')
            assert page.locator(".run-target").inner_text() == "testpg@test"
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')

            page.click('[data-testid="conn-row"][data-db="testpg2"]')  # slow, in-flight
            page.click('[data-testid="conn-row"][data-db="testpg"]')   # fast, resolves first
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')

            page.wait_for_timeout(800)                     # let the stale testpg2 response land
            tables_text = page.locator('[data-testid="schema-tables"]').inner_text()
            assert "synthetic_stale_table" not in tables_text
            assert "customers" in tables_text
        finally:
            ctx.close()


def test_react_result_grid_runs_sql_and_shows_status(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 1 as n, 'ok' as tag")
            page.wait_for_selector('#react-grid td[data-v="ok"]')
            assert page.locator("#react-grid tbody tr").count() == 1
            status = page.locator("#react-status").inner_text()
            assert "1 rows" in status and "testpg@test" in status
        finally:
            ctx.close()


def test_react_grid_sort_third_click_restores_original_order(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select n from (values ('9'),('10'),('2')) as v(n)")
            values = lambda: page.eval_on_selector_all(
                "#react-grid tbody tr td:nth-child(2)", "els => els.map(e => e.textContent)"
            )
            assert values() == ["9", "10", "2"]
            page.locator("#react-grid th .th-btn").first.click()   # asc
            assert values() == ["2", "9", "10"]
            page.locator("#react-grid th .th-btn").first.click()   # desc
            assert values() == ["10", "9", "2"]
            page.locator("#react-grid th .th-btn").first.click()   # restore
            assert values() == ["9", "10", "2"]
            assert page.locator("#react-grid th .arrow", has_text="↑").count() == 0
            assert page.locator("#react-grid th .arrow", has_text="↓").count() == 0
        finally:
            ctx.close()


def test_react_load_more_paginates_truncated_result(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.select_option("#react-max-rows", "100")
            _run_react_sql(page, "select * from generate_series(1,250)")
            assert page.locator("#react-grid tbody tr").count() == 100
            page.wait_for_selector("#react-load-more")
            page.locator("#react-load-more").click()
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 200")
            page.locator("#react-load-more").click()
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 250")
            assert page.locator("#react-load-more").count() == 0
        finally:
            ctx.close()


def test_react_json_modal_and_row_detail(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select '{\"a\":1,\"b\":[1,2]}'::jsonb as doc")
            page.locator("#react-grid td.json").first.dblclick()
            page.wait_for_selector("#react-modal .jt-key")
            assert page.locator("#react-modal .jt-key", has_text="a").count() >= 1
            page.locator("#react-modal button", has_text="Close").click()
            page.wait_for_selector("#react-modal-backdrop", state="detached")
            page.locator("#react-grid td.rownum").first.click()
            page.wait_for_selector("#react-modal pre")
            assert '"a": 1' in page.locator("#react-modal pre").inner_text()
            page.keyboard.press("Escape")
            page.wait_for_selector("#react-modal-backdrop", state="detached")
        finally:
            ctx.close()


def test_react_csv_json_export(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 'a,b' as x, 2 as n")
            with page.expect_download() as dl_csv:
                page.locator("#react-csv-btn").click()
            csv_path = dl_csv.value.path()
            csv_text = Path(csv_path).read_text(encoding="utf-8")
            assert dl_csv.value.suggested_filename == "quarry-testpg.csv"
            assert csv_text.startswith("\ufeff")
            assert '"x","n"' in csv_text and '"a,b"' in csv_text

            with page.expect_download() as dl_json:
                page.locator("#react-json-btn").click()
            payload = json.loads(Path(dl_json.value.path()).read_text(encoding="utf-8"))
            assert dl_json.value.suggested_filename == "quarry-testpg.json"
            assert payload == [{"x": "a,b", "n": 2}]
        finally:
            ctx.close()


def test_react_cell_type_coloring(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(
                page,
                "select 1 as n, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' as u,"
                " '2024-01-02 03:04:05' as ts, true as b, null as z",
            )
            for cls in ("num", "uuid", "ts", "bool", "null"):
                assert page.locator(f"#react-grid td.{cls}").count() >= 1, cls
        finally:
            ctx.close()


def test_react_column_width_drag(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 1 as n, 'ok' as tag")
            th = page.locator("#react-grid thead th.resizable").first
            w0 = th.evaluate("el => el.offsetWidth")
            handle = th.locator(".rz")
            box = handle.bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 80, y, steps=4)
            page.mouse.up()
            w1 = th.evaluate("el => el.offsetWidth")
            assert w1 >= w0 + 40
        finally:
            ctx.close()


def test_react_cell_dblclick_copies_short_value(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1200, "height": 800})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        try:
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector("#react-sql-input", state="visible")
            _run_react_sql(page, "select 'copyme' as v")
            page.locator('#react-grid td[data-v="copyme"]').dblclick()
            page.wait_for_selector("#react-toast", state="visible")
            assert page.evaluate("navigator.clipboard.readText()") == "copyme"
        finally:
            ctx.close()


def test_react_grid_keyboard_nav_and_enter_opens_json_modal(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 1 as n, '{\"a\":1}'::jsonb as doc")
            page.locator("#react-grid tbody tr").first.locator("td").nth(1).click()
            page.wait_for_selector("#react-grid td.sel")
            page.keyboard.press("ArrowRight")
            pos = page.evaluate(
                "(() => { const td = document.querySelector('#react-grid td.sel');"
                "return {col: td.cellIndex}; })()"
            )
            assert pos["col"] == 2
            page.keyboard.press("Enter")
            page.wait_for_selector("#react-modal .jt-key")
            page.keyboard.press("Escape")
            page.wait_for_selector("#react-modal-backdrop", state="detached")
        finally:
            ctx.close()


def test_react_table_click_generates_limit_5_preview(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')
            page.click('[data-testid="schema-tables"] button:has-text("customers")')
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value.includes('customers')"
            )
            assert page.locator("#react-sql-input").input_value() == "select * from customers limit 5"
        finally:
            ctx.close()


def test_react_zero_rows_empty_state(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 1 as n where false")
            page.wait_for_selector('.grid-state:has-text("0 rows")')
        finally:
            ctx.close()


def test_react_network_error_shows_readable_message(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.route("**/api/query", lambda route: route.abort())
            page.fill("#react-sql-input", "select 1")
            page.locator("#react-run-btn").click()
            page.wait_for_selector(".grid-error")
            msg = page.locator(".grid-error").inner_text().strip()
            assert msg and msg != "{}"
        finally:
            ctx.close()


def test_react_sql_highlight_overlay(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.fill("#react-sql-input", "select 'txt' from t -- note")
            hl = page.locator("#react-sql-hl").inner_html()
            assert "tok-kw" in hl and "tok-str" in hl and "tok-cm" in hl
        finally:
            ctx.close()


def test_react_placeholder_states(_pw_browser, tmp_path):
    with _redis_running() as rurl:
        extra = f'\n[testredis]\nurl = "{rurl}"\nengine = "redis"\n'
        with _running_gui(tmp_path, extra_conn=extra) as base:
            ctx, page = _open_react_page(_pw_browser, base)
            try:
                ph0 = page.locator("#react-sql-input").get_attribute("placeholder")
                assert "SQL" in ph0
                page.click('[data-testid="conn-row"][data-db="testredis"]')
                page.wait_for_function(
                    "document.querySelector('#react-sql-input').placeholder.includes('redis')"
                )
            finally:
                ctx.close()


def test_react_ctrl_enter_runs_query(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-sql-input").focus()
            page.keyboard.type("select 1 as n")
            page.keyboard.press("ControlOrMeta+Enter")
            page.wait_for_selector('#react-grid td[data-v="1"]')
        finally:
            ctx.close()


def test_react_history_nav_stashes_and_restores_draft(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 1 as a")
            page.fill("#react-sql-input", "select 999 as unfinished")   # draft on top of history
            page.locator("#react-sql-input").focus()
            page.keyboard.press("ControlOrMeta+ArrowUp")      # walk back -> history entry
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select 1 as a'"
            )
            page.keyboard.press("ControlOrMeta+ArrowDown")    # walk forward -> draft restored
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select 999 as unfinished'"
            )
        finally:
            ctx.close()


def test_react_table_click_preserves_draft_in_history(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.fill("#react-sql-input", "select 123 as draft_marker")    # hand-written, never run
            page.locator('[data-testid="schema-tables"] button:has-text("customers")').click()
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value.includes('customers')"
            )
            # editor now holds the generated preview query…
            assert "from customers" in page.locator("#react-sql-input").input_value()
            # …and the draft is recoverable from History
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-panel .hist-item")
            assert page.locator("#react-history-panel .hist-item", has_text="draft_marker").count() == 1
        finally:
            ctx.close()


def test_react_history_recall_then_overwrite_preserves_original_draft(_pw_browser, tmp_path):
    """Regression: recalling a history entry (Cmd/Ctrl+↑) and then triggering a
    SECOND overwrite (e.g. a table click) must not silently drop the original
    hand-written draft that was on-screen before the recall."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 1 as a")                       # history now has one entry
            page.fill("#react-sql-input", "select 999 as unfinished")   # draft, never run
            page.locator("#react-sql-input").focus()
            page.keyboard.press("ControlOrMeta+ArrowUp")                # recall -> draft only in memory now
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select 1 as a'"
            )
            page.locator('[data-testid="schema-tables"] button:has-text("customers")').click()
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value.includes('customers')"
            )
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-panel .hist-item")
            assert page.locator("#react-history-panel .hist-item", has_text="unfinished").count() == 1
        finally:
            ctx.close()


def test_react_autocomplete_keyword(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-sql-input").focus()
            page.keyboard.type("sele")
            page.wait_for_selector(".acbox .acitem")
            assert page.locator(".acbox .acitem .ack-kw").count() >= 1
            page.keyboard.press("Tab")
            page.wait_for_function("document.querySelector('#react-sql-input').value === 'SELECT'")
        finally:
            ctx.close()


def test_react_autocomplete_table_and_from_narrows(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-sql-input").focus()
            page.keyboard.type("select * from cus")
            page.wait_for_selector(".acbox .acitem")
            kinds = page.eval_on_selector_all(".acbox .acitem .ack", "els => els.map(e => e.textContent)")
            assert kinds and all(k == "tbl" for k in kinds)     # after FROM: tables only
            page.locator(".acbox .acitem", has_text="customers").click()
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select * from customers'"
            )
        finally:
            ctx.close()


def test_react_autocomplete_table_dot_column(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-sql-input").focus()
            page.keyboard.type("select customers.")
            page.wait_for_selector(".acbox .acitem .ack-col", timeout=8000)
            items = page.locator(".acbox .acitem").all_inner_texts()
            assert any("email" in it for it in items)
            page.keyboard.press("Escape")
            page.wait_for_selector(".acbox", state="detached")
        finally:
            ctx.close()


def test_react_editor_height_drag_persists(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            h0 = page.evaluate("document.querySelector('.edwrap').offsetHeight")
            box = page.locator(".ed-resizer").bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x, y + 60, steps=4)
            page.mouse.up()
            h1 = page.evaluate("document.querySelector('.edwrap').offsetHeight")
            assert h1 >= h0 + 40
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input", state="visible")
            assert abs(page.evaluate("document.querySelector('.edwrap').offsetHeight") - h1) <= 2
        finally:
            ctx.close()


GROUPED_TOML = f'\n[shopgrp]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\ngroup = "acme"\n'


def test_react_sidebar_group_collapse_persists_across_reload(_pw_browser, tmp_path):
    """issue #49: connection groups collapse/expand and remember state across a reload."""
    with _running_gui(tmp_path, extra_conn=GROUPED_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="conn-row"][data-db="shopgrp"]')
            toggle = page.locator('[data-testid="conn-group-toggle"]', has_text="acme")
            toggle.click()
            page.wait_for_selector('[data-testid="conn-row"][data-db="shopgrp"]', state="detached")

            page.reload(wait_until="networkidle")
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"]')
            assert page.locator('[data-testid="conn-row"][data-db="shopgrp"]').count() == 0

            page.locator('[data-testid="conn-group-toggle"]', has_text="acme").click()
            page.wait_for_selector('[data-testid="conn-row"][data-db="shopgrp"]')
        finally:
            ctx.close()


def test_react_health_dots_paint_from_cache_and_manual_check(_pw_browser, tmp_path):
    """issue #49: health dots paint instantly from the cache on load, and a manual
    check probes every connection (ok vs down, with an error tooltip on down rows)."""
    with _running_gui(tmp_path, extra_conn=DEAD_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-health-btn").click()
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"] .health-dot.ok', timeout=20000)
            page.wait_for_selector('[data-testid="conn-row"][data-db="deadpg"] .health-dot.down', timeout=20000)
            row = page.locator('[data-testid="conn-row"][data-db="deadpg"]')
            assert "down" in row.get_attribute("class").split()
            assert (row.get_attribute("title") or "").strip()

            page.reload(wait_until="networkidle")
            # no click this time: dots repaint straight from the backend health cache
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"] .health-dot.ok', timeout=10000)
        finally:
            ctx.close()


ENVSET_TOML = f"""
[shop_dev]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "dev"
db = "shop"

[shop_prod]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "prod"
db = "shop"
"""


def test_react_env_pill_prod_skips_autorun_nonprod_reruns(_pw_browser, tmp_path):
    """issue #49: switching envs via a pill re-runs the current query — EXCEPT
    switching to prod, which must never auto-fire it."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            assert "prod" in page.locator(
                '[data-testid="env-pills"] .env-pill[data-env="prod"]'
            ).get_attribute("class")

            _run_react_sql(page, "select 1 as a")

            requests: list[str] = []
            page.on("request", lambda r: "/api/query" in r.url and requests.append(r.url))

            page.locator('[data-testid="env-pills"] .env-pill[data-env="prod"]').click()
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('prod')")
            page.wait_for_timeout(500)
            assert requests == []                                    # no auto-run on prod
            assert page.locator("#react-grid tbody tr").count() == 1  # old result still painted

            page.locator('[data-testid="env-pills"] .env-pill[data-env="dev"]').click()
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('dev')")
            page.wait_for_timeout(500)
            assert len(requests) == 1                                # switching off prod auto-reran
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# issue #51: unified connection-tag contract — every result is tagged with the
# connection that PRODUCED it, and a request in flight is only ever applied to
# (or errors on) the tab that fired it, while its issuing connection still
# matches. React port of the invariants covered for the legacy GUI in
# test_gui_browser_features.py's tab-isolation section (issue #18).
# ---------------------------------------------------------------------------


def test_react_tab_switch_isolates_result_grid_between_tabs(_pw_browser, tmp_path):
    """Each tab's grid is its own store entry — switching to a tab with no
    result yet must show the empty placeholder, never a stale grid carried
    over from whichever tab was active before, and switching back restores it."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 111 as tab1_marker")
            page.wait_for_selector('#react-grid td[data-v="111"]')

            page.locator("#react-tab-add").click()  # new blank tab, no result yet
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            assert page.locator("#react-grid").count() == 0
            assert page.locator(".grid-state", has_text="run a query").count() == 1

            page.locator("#react-tabs [data-testid=tab]").nth(0).click()  # back to tab 1
            page.wait_for_selector('#react-grid td[data-v="111"]')
        finally:
            ctx.close()


_DELAY_QUERY_INIT_SCRIPT_TMPL = """
(() => {{
    const origFetch = window.fetch;
    window.fetch = (input, init) => {{
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/api/query") && init && typeof init.body === "string"
            && init.body.includes("{marker}")) {{
            return new Promise((resolve) => {{
                setTimeout(() => resolve(origFetch(input, init)), 700);
            }});
        }}
        return origFetch(input, init);
    }};
}})();
"""


def test_react_inflight_response_lands_on_origin_tab_not_newly_active_tab(_pw_browser, tmp_path):
    """A request fired from tab A must resolve into tab A's OWN result slot
    even if the user has since switched to tab B — it must never repaint
    whichever tab happens to be active when the response lands.

    The delay is an in-page fetch override matching the request BODY (every
    /api/query call hits the same URL), same anti-serialization technique as
    the schema-browser stale-response tests above.
    """
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.add_init_script(_DELAY_QUERY_INIT_SCRIPT_TMPL.format(marker="tabA_slow"))
            page.fill("#react-sql-input", "select 77 as tabA_slow")
            page.locator("#react-run-btn").click()  # slow, in flight on tab A
            page.wait_for_selector('.grid-state:has-text("running query")')

            page.locator("#react-tab-add").click()  # switch to a fresh tab B
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            assert page.locator(".grid-state", has_text="run a query").count() == 1  # B starts clean

            page.wait_for_timeout(900)  # let tab A's slow response land
            assert page.locator("#react-grid td[data-v='77']").count() == 0  # not painted onto B
            assert page.locator(".grid-state", has_text="run a query").count() == 1

            page.locator("#react-tabs [data-testid=tab]").nth(0).click()  # back to tab A
            page.wait_for_selector("#react-grid td[data-v='77']")  # A's own result is there
        finally:
            ctx.close()


def test_react_result_not_restored_after_tab_rebound_to_different_connection(_pw_browser, tmp_path):
    """A result is tagged with its PRODUCING connection, not whatever
    connection the tab is later re-pointed to — rebinding the tab via an env
    pill (no autorun) must not let the old grid survive a reload under the
    new connection."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            _run_react_sql(page, "select 42 as dev_only")
            page.wait_for_selector('#react-grid td[data-v="42"]')

            page.locator('[data-testid="env-pills"] .env-pill[data-env="prod"]').click()  # rebind, no autorun
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('prod')")

            tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabs')).tabs")
            saved = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabres'))")
            # the persisted result is tagged with its PRODUCING connection (dev),
            # not the tab's current (prod) one — so it can't masquerade as prod data
            assert saved[tabs[0]["id"]]["queryEnv"] == "dev"

            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.wait_for_function("document.querySelector('.run-target')?.textContent.includes('prod')")
            page.wait_for_timeout(300)
            # tab reloads bound to prod; the dev-tagged result no longer matches,
            # so the grid comes back empty instead of restoring dev rows mislabeled as prod
            assert page.locator('#react-grid td[data-v="42"]').count() == 0
        finally:
            ctx.close()


def test_react_result_stays_until_tab_switch_then_clears_on_return_after_rebind(_pw_browser, tmp_path):
    """An in-place connection switch (env pill, same tab stays active) must
    never touch the currently-painted grid — but leaving the tab and coming
    back must re-validate it against the tab's (now different) connection,
    same as a reload would."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            _run_react_sql(page, "select 42 as dev_only")
            page.wait_for_selector('#react-grid td[data-v="42"]')

            page.locator('[data-testid="env-pills"] .env-pill[data-env="prod"]').click()  # rebind, no autorun
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('prod')")
            page.wait_for_timeout(200)
            assert page.locator('#react-grid td[data-v="42"]').count() == 1  # untouched while still active

            page.locator("#react-tab-add").click()  # leave tab 1 for a fresh tab 2
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.locator("#react-tabs [data-testid=tab]").nth(0).click()  # back to tab 1 (now bound to prod)
            page.wait_for_function("document.querySelector('.run-target')?.textContent.includes('prod')")
            assert page.locator('#react-grid td[data-v="42"]').count() == 0  # re-validated away on return
        finally:
            ctx.close()


def test_react_inflight_response_dropped_when_same_tab_switches_connection_mid_flight(
    _pw_browser, tmp_path
):
    """A request in flight whose OWN tab is re-pointed to another connection
    before it resolves must be dropped — never repainted, and never persisted,
    as if it belonged to the new connection."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.add_init_script(_DELAY_QUERY_INIT_SCRIPT_TMPL.format(marker="devval_slow"))
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            page.fill("#react-sql-input", "select 42 as devval_slow")
            page.locator("#react-run-btn").click()  # slow, in flight on shop@dev
            page.wait_for_selector('.grid-state:has-text("running query")')

            page.locator('[data-testid="env-pills"] .env-pill[data-env="prod"]').click()  # same tab -> prod
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('prod')")
            page.wait_for_timeout(900)  # let the dev response land

            assert page.locator('#react-grid td[data-v="42"]').count() == 0
            tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabs')).tabs")
            saved = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabres') || '{}')")
            assert tabs[0]["id"] not in saved  # nothing persisted for the tab either
        finally:
            ctx.close()


def test_react_saved_query_result_persisted_under_producing_connection(_pw_browser, tmp_path):
    """A saved query runs on ITS OWN connection (testpg). Launched from a tab
    bound to a different connection (shop@dev), the tab must be re-pointed to
    the producing connection so the result is correctly tagged, persisted, and
    restored after a reload — not orphaned under the tab's connection at
    launch time."""
    paramless = "-- @name: all-cust\n-- @db: testpg\nSELECT * FROM customers ORDER BY id\n"
    with _running_gui(
        tmp_path, extra_conn=ENVSET_TOML, seed_queries={"all-cust": paramless}
    ) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()  # bind tab to shop@dev
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            page.wait_for_selector('[data-testid="saved-query-item"]', timeout=10000)
            page.locator('[data-testid="saved-query-item"]', has_text="all-cust").click()
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 3")
            page.wait_for_function("document.querySelector('.run-target')?.textContent === 'testpg@test'")

            tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabs')).tabs")
            assert tabs[0]["db"] == "testpg" and tabs[0]["env"] == "test"  # tab re-pointed to producing conn
            saved = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabres'))")
            assert saved[tabs[0]["id"]]["queryDb"] == "testpg"

            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.wait_for_function("document.querySelector('.run-target')?.textContent === 'testpg@test'")
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 3")
        finally:
            ctx.close()


REDIS_TREE_KEYS = ["qygui:sess:1", "qygui:sess:2", "qygui:jobs"]


def test_react_redis_tree_badges_filter_and_inspect(_pw_browser, tmp_path):
    """issue #49: redis key tree folds by ':', shows type/TTL badges, narrows
    with the filter box, and clicking a leaf key inspects it into the grid."""
    with _redis_running() as rurl:
        _rcli(rurl, "del", *REDIS_TREE_KEYS)
        _rcli(rurl, "set", "qygui:sess:1", "alpha", "EX", "3600")
        _rcli(rurl, "set", "qygui:sess:2", "beta")
        _rcli(rurl, "rpush", "qygui:jobs", "a", "b")
        extra = f'\n[testredis]\nurl = "{rurl}"\nengine = "redis"\n'
        try:
            with _running_gui(tmp_path, extra_conn=extra) as base:
                ctx, page = _open_react_page(_pw_browser, base)
                try:
                    page.locator('[data-testid="conn-row"][data-db="testredis"]').click()
                    # tree starts fully expanded, so leaf keys are visible without any clicks
                    page.wait_for_selector('[data-testid="redis-key"][data-key="qygui:jobs"]', timeout=15000)

                    jobs = page.locator('[data-testid="redis-key"][data-key="qygui:jobs"]')
                    assert "list" in jobs.locator(".rbadge").first.inner_text()

                    sess1 = page.locator('[data-testid="redis-key"][data-key="qygui:sess:1"]')
                    page.wait_for_selector('[data-testid="redis-key"][data-key="qygui:sess:1"]')
                    assert sess1.locator(".rbadge.ttl").count() == 1

                    # folding: clicking the "qygui" dir collapses its children, click again to re-expand
                    page.locator('[data-testid="redis-dir"]', has_text="qygui").click()
                    page.wait_for_selector('[data-testid="redis-key"][data-key="qygui:jobs"]', state="detached")
                    page.locator('[data-testid="redis-dir"]', has_text="qygui").click()
                    page.wait_for_selector('[data-testid="redis-key"][data-key="qygui:jobs"]')

                    page.fill(".table-filter", "jobs")
                    page.wait_for_selector(
                        '[data-testid="redis-key"][data-key="qygui:sess:1"]', state="detached"
                    )
                    assert page.locator('[data-testid="redis-key"][data-key="qygui:jobs"]').count() == 1
                    page.fill(".table-filter", "")
                    page.wait_for_selector('[data-testid="redis-key"][data-key="qygui:sess:1"]')

                    page.locator('[data-testid="redis-key"][data-key="qygui:sess:1"]').click()
                    page.wait_for_selector("#react-grid tbody tr")
                    assert page.locator("#react-sql-input").input_value() == "# qygui:sess:1"
                finally:
                    ctx.close()
        finally:
            _rcli(rurl, "del", *REDIS_TREE_KEYS)


def test_react_redis_capped_key_list_shows_notice(_pw_browser, tmp_path):
    """issue #49: a redis connection with >400 keys shows a capped-list notice."""
    with _redis_running() as rurl:
        keys = [f"qycap:{i}" for i in range(401)]
        _rcli(rurl, "mset", *[a for k in keys for a in (k, "x")])
        extra = f'\n[testredis]\nurl = "{rurl}"\nengine = "redis"\n'
        try:
            with _running_gui(tmp_path, extra_conn=extra) as base:
                ctx, page = _open_react_page(_pw_browser, base)
                try:
                    page.locator('[data-testid="conn-row"][data-db="testredis"]').click()
                    page.wait_for_selector(
                        '[data-testid="schema-tables"] p:has-text("first")', timeout=20000
                    )
                    text = page.locator('[data-testid="schema-tables"]').inner_text()
                    assert any(ch.isdigit() for ch in text)
                finally:
                    ctx.close()
        finally:
            _rcli(rurl, "del", *keys)


def test_react_saved_query_run_preserves_draft_in_history(_pw_browser, tmp_path):
    """Regression for PR #58 review: running a saved query (param-less or via
    the param modal) must stash a hand-written, never-run draft into History
    instead of silently discarding it, same as table click / key inspect."""
    paramless = "-- @name: all-cust\n-- @db: testpg\nSELECT * FROM customers ORDER BY id\n"
    with _running_gui(tmp_path, seed_queries={"all-cust": paramless}) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="saved-query-item"]', timeout=10000)
            page.fill("#react-sql-input", "select 456 as draft_marker")  # hand-written, never run
            page.locator('[data-testid="saved-query-item"]', has_text="all-cust").click()
            page.wait_for_function(
                "document.querySelectorAll('#react-grid tbody tr').length === 3"
            )
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-panel .hist-item")
            assert page.locator("#react-history-panel .hist-item", has_text="draft_marker").count() == 1
        finally:
            ctx.close()


def test_react_saved_queries_paramless_run_and_param_modal(_pw_browser, tmp_path):
    """issue #49: a param-less saved query runs straight away on click; one with
    params opens a modal, pre-filling defaults, and Enter submits it."""
    paramless = "-- @name: all-cust\n-- @db: testpg\nSELECT * FROM customers ORDER BY id\n"
    withparam = (
        "-- @name: cust-by-id\n-- @db: testpg\n"
        "-- @param: id (int, required)\n-- @param: note (text, default=hi)\n"
        "SELECT * FROM customers WHERE id = :id\n"
    )
    with _running_gui(
        tmp_path, seed_queries={"all-cust": paramless, "cust-by-id": withparam}
    ) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="saved-query-item"]', timeout=10000)

            page.locator('[data-testid="saved-query-item"]', has_text="all-cust").click()
            page.wait_for_selector("#react-grid tbody tr")
            assert page.locator("#saved-query-modal").count() == 0
            assert page.locator("#react-grid tbody tr").count() == 3

            page.locator('[data-testid="saved-query-item"]', has_text="cust-by-id").click()
            page.wait_for_selector("#saved-query-modal")
            assert page.locator('[data-testid="saved-query-param-note"]').input_value() == "hi"
            page.fill('[data-testid="saved-query-param-id"]', "1")
            page.keyboard.press("Enter")
            page.wait_for_selector("#saved-query-modal", state="detached")
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 1")
        finally:
            ctx.close()


def test_react_saved_query_modal_closes_on_clickout(_pw_browser, tmp_path):
    withparam = "-- @name: cust-by-id\n-- @db: testpg\n-- @param: id (int, required)\nSELECT * FROM customers WHERE id = :id\n"
    with _running_gui(tmp_path, seed_queries={"cust-by-id": withparam}) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="saved-query-item"]')
            page.locator('[data-testid="saved-query-item"]', has_text="cust-by-id").click()
            page.wait_for_selector("#saved-query-modal")
            page.locator("#saved-query-modal-backdrop").click(position={"x": 5, "y": 5})
            page.wait_for_selector("#saved-query-modal", state="detached")
        finally:
            ctx.close()


def test_react_sidebar_width_drag_persists(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            w0 = page.evaluate("document.querySelector('.conn-side').offsetWidth")
            box = page.locator("#sidebar-resizer").bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 80, y, steps=4)
            page.mouse.up()
            w1 = page.evaluate("document.querySelector('.conn-side').offsetWidth")
            assert w1 >= w0 + 60
            page.reload(wait_until="networkidle")
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"]')
            assert abs(page.evaluate("document.querySelector('.conn-side').offsetWidth") - w1) <= 2
        finally:
            ctx.close()


def test_react_table_refresh_preserves_filter_text(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')
            page.fill(".table-filter", "cust")
            page.wait_for_selector(
                '[data-testid="schema-tables"] button:has-text("orders")', state="detached"
            )
            page.locator('[data-testid="table-refresh-btn"]').click()
            page.wait_for_selector('[data-testid="schema-tables"] button:has-text("customers")')
            assert page.locator(".table-filter").input_value() == "cust"
            assert page.locator('[data-testid="schema-tables"] button:has-text("orders")').count() == 0
        finally:
            ctx.close()


def _drag_react_tab(page, from_id, to_id):
    """Dispatch real dragstart/dragover/drop DOM events between two tabs, the
    same sequence a mouse drag produces, so this exercises the production
    ondragstart/ondragover/ondrop handlers rather than calling an internal fn."""
    page.evaluate(
        """([from, to]) => {
            const src = document.querySelector(`#react-tabs [data-testid=tab][data-tab-id="${from}"]`);
            const dst = document.querySelector(`#react-tabs [data-testid=tab][data-tab-id="${to}"]`);
            const dt = new DataTransfer();
            src.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
            dst.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt}));
            dst.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
            src.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
        }""",
        [from_id, to_id],
    )


def test_react_tab_add_switch_close_and_persist(_pw_browser, tmp_path):
    """issue #50: tabs add/switch/close, each with its own SQL, persisted across a reload."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            assert page.locator("#react-tabs [data-testid=tab]").count() == 1
            assert page.locator('#react-tabs [data-testid="tab-close"]').count() == 0  # sole tab: no ×

            page.fill("#react-sql-input", "select 1 as a")
            page.locator("#react-tab-add").click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            assert page.input_value("#react-sql-input") == ""  # new tab starts blank

            page.fill("#react-sql-input", "select 2 as b")
            page.locator("#react-tabs [data-testid=tab]").nth(0).click()
            page.wait_for_function("document.querySelector('#react-sql-input').value === 'select 1 as a'")
            page.locator("#react-tabs [data-testid=tab]").nth(1).click()
            page.wait_for_function("document.querySelector('#react-sql-input').value === 'select 2 as b'")

            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            assert page.input_value("#react-sql-input") == "select 2 as b"  # active tab survived reload

            page.locator("#react-tabs [data-testid=tab]").nth(1).locator('[data-testid="tab-close"]').click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 1")
            page.wait_for_function("document.querySelector('#react-sql-input').value === 'select 1 as a'")
            assert page.locator('#react-tabs [data-testid="tab-close"]').count() == 0
        finally:
            ctx.close()


def test_react_tab_title_shows_db_at_env_and_rename(_pw_browser, tmp_path):
    """issue #50: auto title is db@env; rename commits on Enter/blur, reverts on Escape,
    an empty name reverts to the auto title, and a custom title survives a reload."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_function(
                "document.querySelector('#react-tabs [data-testid=tab] .lbl')?.textContent === 'testpg@test'"
            )
            tab = page.locator("#react-tabs [data-testid=tab]").first

            tab.dblclick()
            page.fill('[data-testid="tab-rename-input"]', "scratch title")
            page.keyboard.press("Escape")
            page.wait_for_function(
                "document.querySelector('#react-tabs [data-testid=tab] .lbl')?.textContent === 'testpg@test'"
            )

            tab.dblclick()
            page.fill('[data-testid="tab-rename-input"]', "blur title")
            page.locator("#react-sql-input").click()  # blur (no Enter) must also commit
            page.wait_for_function(
                "document.querySelector('#react-tabs [data-testid=tab] .lbl')?.textContent === 'blur title'"
            )

            tab.dblclick()
            page.fill('[data-testid="tab-rename-input"]', "kept title")
            page.keyboard.press("Enter")
            page.wait_for_function(
                "document.querySelector('#react-tabs [data-testid=tab] .lbl')?.textContent === 'kept title'"
            )

            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.wait_for_function(
                "document.querySelector('#react-tabs [data-testid=tab] .lbl')?.textContent === 'kept title'"
            )

            tab = page.locator("#react-tabs [data-testid=tab]").first
            tab.dblclick()
            page.fill('[data-testid="tab-rename-input"]', "")
            page.keyboard.press("Enter")
            page.wait_for_function(
                "document.querySelector('#react-tabs [data-testid=tab] .lbl')?.textContent === 'testpg@test'"
            )
        finally:
            ctx.close()


def test_react_tab_close_preserves_sql_in_history(_pw_browser, tmp_path):
    """issue #50: closing a tab (active or inactive) must never silently lose hand-written SQL."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.fill("#react-sql-input", "select 111 as keepme_inactive")  # tab 1 draft, never run
            page.locator("#react-tab-add").click()  # -> tab 2 active
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.locator("#react-tabs [data-testid=tab]").nth(0).locator('[data-testid="tab-close"]').click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 1")

            page.fill("#react-sql-input", "select 222 as keepme_active")  # draft in remaining tab
            page.locator("#react-tab-add").click()  # new empty tab active
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.locator("#react-tabs [data-testid=tab]").nth(0).click()  # back to the draft tab
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select 222 as keepme_active'"
            )
            page.locator("#react-tabs [data-testid=tab].on").locator('[data-testid="tab-close"]').click()

            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-panel .hist-item")
            texts = page.locator("#react-history-panel .hist-item").all_inner_texts()
            assert any("keepme_inactive" in t for t in texts)
            assert any("keepme_active" in t for t in texts)
        finally:
            ctx.close()


def test_react_tab_drag_reorder_moves_active_tab(_pw_browser, tmp_path):
    """issue #50: drag-and-drop reorders tabs; the active tab follows its id, not its old index."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.fill("#react-sql-input", "select 1 as a")
            page.locator("#react-tab-add").click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.fill("#react-sql-input", "select 2 as b")
            page.locator("#react-tab-add").click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 3")
            page.fill("#react-sql-input", "select 3 as c")

            ids = page.evaluate(
                "[...document.querySelectorAll('#react-tabs [data-testid=tab]')].map(t => t.dataset.tabId)"
            )
            _drag_react_tab(page, ids[2], ids[0])  # drag the active (3rd) tab to the front

            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select 3 as c'"
            )
            first = page.locator("#react-tabs [data-testid=tab]").first
            assert "on" in (first.get_attribute("class") or "")
            assert first.get_attribute("data-tab-id") == ids[2]

            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            reordered = page.evaluate(
                "[...document.querySelectorAll('#react-tabs [data-testid=tab]')].map(t => t.dataset.tabId)"
            )
            assert reordered == [ids[2], ids[0], ids[1]]
        finally:
            ctx.close()


def test_react_tab_middle_click_closes(_pw_browser, tmp_path):
    """issue #50: middle-click closes a tab, same as the × glyph; a no-op on the last tab."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-tab-add").click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.locator("#react-tabs [data-testid=tab]").first.click(button="middle")
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 1")
            page.locator("#react-tabs [data-testid=tab]").first.click(button="middle")
            page.wait_for_timeout(150)
            assert page.locator("#react-tabs [data-testid=tab]").count() == 1
        finally:
            ctx.close()


def test_react_tab_keyboard_shortcut_closes_active_tab(_pw_browser, tmp_path):
    """issue #50: Cmd/Ctrl+Shift+W closes the active tab; a no-op when it is the only tab left."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-tab-add").click()
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.keyboard.press("Control+Shift+W")
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 1")
            page.keyboard.press("Control+Shift+W")
            page.wait_for_timeout(150)
            assert page.locator("#react-tabs [data-testid=tab]").count() == 1
        finally:
            ctx.close()


@pytest.mark.integration
def test_api_version(gui_server):
    code, body = gui_server.get("/api/version")
    assert code == 200
    assert body == {"name": "Quarry", "version": __version__}


@pytest.mark.integration
def test_react_app_index_served(gui_server):
    with urllib.request.urlopen(gui_server.base + "/app/") as resp:
        html = resp.read().decode()
    assert resp.status == 200
    assert 'id="root"' in html


@pytest.mark.unit
def test_wheel_includes_web_dist(tmp_path):
    """Built wheel must ship the pre-built React assets (zero Node at install time)."""
    import subprocess
    import sys

    pytest.importorskip("build")
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert wheels, "expected a wheel in dist/"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert any(n.startswith("quarry/web_dist/") and n.endswith("index.html") for n in names)


@pytest.mark.unit
def test_sdist_excludes_node_modules(tmp_path):
    """sdist must not ship npm install trees — Node is dev/CI-only."""
    import subprocess
    import sys
    import tarfile

    pytest.importorskip("build")
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    sdists = list(dist.glob("*.tar.gz"))
    assert sdists, "expected an sdist in dist/"
    with tarfile.open(sdists[0], "r:gz") as tf:
        names = tf.getnames()
    assert not any("node_modules" in n for n in names)
    assert not any(n.endswith("tsconfig.tsbuildinfo") for n in names)
