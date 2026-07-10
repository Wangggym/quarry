"""Browser + API checks for the React GUI at /app — the only frontend quarry
ships; `gui.py` is backend-only (http.server + serving the built web_dist).
"""

from __future__ import annotations

import re
import urllib.request
import zipfile
import json
from pathlib import Path

import pytest

from conftest import DEAD_TOML, REPO, TEST_DB_URL, _rcli, _redis_running, _running_gui, requires_browser
from quarry import __version__

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
            page.wait_for_selector("#react-history-modal .hist-item")
            assert page.locator("#react-history-modal .hist-item", has_text="draft_marker").count() == 1
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
            page.wait_for_selector("#react-history-modal .hist-item")
            assert page.locator("#react-history-modal .hist-item", has_text="unfinished").count() == 1
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


def test_react_load_more_disabled_after_inplace_connection_rebind(_pw_browser, tmp_path):
    """The stale-grid-stays-visible behavior above must not extend to "Load
    more": once the tab's current connection has drifted from the one that
    produced the shown (truncated) page, pagination must be hidden rather
    than fetch the next page from the connection the tab no longer points
    at."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            page.select_option("#react-max-rows", "100")
            _run_react_sql(page, "select * from generate_series(1,250)")
            page.wait_for_selector("#react-load-more")

            page.locator('[data-testid="env-pills"] .env-pill[data-env="prod"]').click()  # rebind, no autorun
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('prod')")
            assert page.locator("#react-grid tbody tr").count() == 100  # grid itself still untouched
            assert page.locator("#react-load-more").count() == 0  # but pagination on the old page is gone
        finally:
            ctx.close()


def test_react_background_tab_error_surfaces_when_returned_to(_pw_browser, tmp_path):
    """An error is tagged and persisted per-tab exactly like a successful
    result: a query that fails while its tab is in the background must not
    be silently swallowed — it surfaces once the user returns to that tab."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.add_init_script(_DELAY_QUERY_INIT_SCRIPT_TMPL.format(marker="will_fail_slow"))
            page.fill("#react-sql-input", "select * from no_such_table_at_all_will_fail_slow")
            page.locator("#react-run-btn").click()  # slow + fated to fail, in flight on tab 1
            page.wait_for_selector('.grid-state:has-text("running query")')

            page.locator("#react-tab-add").click()  # leave tab 1 before the failure lands
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            page.wait_for_timeout(900)  # let the failed response land while tab 1 is in the background
            assert page.locator(".grid-error").count() == 0  # tab 2 (active) shows no error of its own

            page.locator("#react-tabs [data-testid=tab]").nth(0).click()  # back to tab 1
            page.wait_for_selector(".grid-error")  # the earlier failure surfaces now, not silently dropped
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


def test_react_saved_query_with_logical_envset_db_retargets_tab(_pw_browser, tmp_path):
    """#18/#51: the producing-connection tagging above must also hold when the
    saved query's `@db` is itself a LOGICAL env-set name (`shop`), not a
    concrete connection key — resolved via `core.resolve_connection`'s
    env-set lookup branch instead of its direct-key shortcut. Launched from a
    tab bound to an unrelated single-env connection that happens to share
    `env=dev` with the `shop` env-set, the tab must be re-pointed to
    `shop@dev` (what the saved query actually resolved to and ran on), not
    left bound to its launch-time connection."""
    extra = ENVSET_TOML + f"""
[billing_dev]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "dev"
db = "billing"
"""
    saved = "-- @name: shop-probe\n-- @db: shop\nSELECT 77 AS shop_probe\n"
    with _running_gui(tmp_path, extra_conn=extra, seed_queries={"shop-probe": saved}) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="billing"]').click()  # bind tab to billing@dev
            page.wait_for_function("document.querySelector('.run-target')?.textContent === 'billing@dev'")
            page.wait_for_selector('[data-testid="saved-query-item"]', timeout=10000)
            page.locator('[data-testid="saved-query-item"]', has_text="shop-probe").click()
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 1")
            page.wait_for_function("document.querySelector('.run-target')?.textContent === 'shop@dev'")

            tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabs')).tabs")
            assert tabs[0]["db"] == "shop" and tabs[0]["env"] == "dev"  # re-pointed, not left on billing@dev
            saved_state = page.evaluate("JSON.parse(localStorage.getItem('qy_react_tabres'))")
            assert saved_state[tabs[0]["id"]]["queryDb"] == "shop"

            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.wait_for_function("document.querySelector('.run-target')?.textContent === 'shop@dev'")
            page.wait_for_function("document.querySelectorAll('#react-grid tbody tr').length === 1")
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
            page.wait_for_selector("#react-history-modal .hist-item")
            assert page.locator("#react-history-modal .hist-item", has_text="draft_marker").count() == 1
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
            page.wait_for_selector("#react-history-modal .hist-item")
            texts = page.locator("#react-history-modal .hist-item").all_inner_texts()
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


# ---------------------------------------------------------------------------
# issue #52: header + toolbar parity — workspace label/badges, lang/theme
# toggles, connection-info + workspace-manager modals, Format/EXPLAIN
# buttons, and the History-panel-to-modal upgrade.
# ---------------------------------------------------------------------------

def test_react_header_shows_workspace_label_and_readonly_badge(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            label = page.locator('[data-testid="ws-label"]')
            label.wait_for(state="visible")
            assert label.inner_text().strip() != ""
            assert label.get_attribute("title") == label.inner_text()
            ro = page.locator("#react-ro-badge")
            ro.wait_for(state="visible")
            assert "read-only" in ro.inner_text().lower()
            assert page.locator("#react-prod-badge").count() == 0
        finally:
            ctx.close()


def test_react_header_prod_badge_shows_for_prod_env_only(_pw_browser, tmp_path):
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shop"]').click()
            page.wait_for_selector('[data-testid="env-pills"] .env-pill[data-env="dev"].on')
            assert page.locator("#react-prod-badge").count() == 0
            page.locator('[data-testid="env-pills"] .env-pill[data-env="prod"]').click()
            page.wait_for_selector("#react-prod-badge")
            assert page.locator("#react-prod-badge").inner_text() == "prod"
        finally:
            ctx.close()


def test_react_header_language_toggle_persists(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            assert "read-only" in page.locator("#react-ro-badge").inner_text().lower()
            assert page.locator("#react-run-btn").inner_text() == "Run"
            assert page.locator("#react-format-btn").inner_text() == "Format"
            assert page.locator("#react-lang-btn").inner_text() == "中"
            page.locator("#react-lang-btn").click()
            page.wait_for_function(
                "document.querySelector('#react-ro-badge').textContent.includes('只读')"
            )
            assert page.locator("#react-lang-btn").inner_text() == "EN"
            # toolbar + History modal chrome must flip too, not just the header
            # badge (regression: these were hardcoded English despite the
            # i18n dictionary already having zh entries for them)
            assert page.locator("#react-run-btn").inner_text() == "运行"
            assert page.locator("#react-format-btn").inner_text() == "格式化"
            assert "历史" in page.locator("#react-history-btn").inner_text()
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-modal")
            assert page.locator("#react-history-search").get_attribute("placeholder") == "搜索历史…"
            assert page.locator('[data-testid="history-empty"]').inner_text() == "暂无历史记录"
            page.locator("#react-history-modal button", has_text="关闭").click()
            page.wait_for_selector("#react-history-modal", state="detached")
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            assert "只读" in page.locator("#react-ro-badge").inner_text()
            assert page.locator("#react-run-btn").inner_text() == "运行"
        finally:
            ctx.close()


def test_react_header_theme_toggle_persists(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            before = page.evaluate("document.documentElement.dataset.theme")
            assert before == "light"
            page.locator("#react-theme-btn").click()
            page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            page.locator("#react-theme-btn").click()  # restore for a clean localStorage
            page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        finally:
            ctx.close()


def test_react_conninfo_modal_shows_masked_url_and_health(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            assert page.locator("#react-conninfo-btn").count() == 1
            page.locator("#react-conninfo-btn").click()
            page.wait_for_selector('[data-testid="conninfo-url"]')
            body = page.locator("#react-conninfo-modal").inner_text()
            assert "testpg" in body
            m = re.match(r".*://[^:/@]+:([^@]+)@", TEST_DB_URL)
            if m:
                assert f":{m.group(1)}@" not in body
                assert "••••" in body
            page.wait_for_selector("#react-conninfo-health.ok", timeout=10000)
            page.mouse.click(5, 5)  # click outside closes, same as every other modal
            assert page.locator("#react-conninfo-modal").count() == 0
        finally:
            ctx.close()


def test_react_conninfo_reveal_and_copy_real_url(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1200, "height": 800})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        try:
            page.goto(f"{base}/app/", wait_until="networkidle")
            page.wait_for_selector("#react-sql-input", state="visible")
            page.locator("#react-conninfo-btn").click()
            page.wait_for_selector('[data-testid="conninfo-url"]')
            assert page.locator("#react-conninfo-reveal").inner_text() == "reveal"
            page.locator("#react-conninfo-reveal").click()  # eye toggles masked -> revealed
            page.wait_for_function("document.querySelector('#react-conninfo-reveal').textContent === 'hide'")
            assert page.locator('[data-testid="conninfo-url"]').inner_text() == TEST_DB_URL
            page.locator("#react-conninfo-reveal").click()  # and back
            page.wait_for_function("document.querySelector('#react-conninfo-reveal').textContent === 'reveal'")
            # copy always re-fetches with reveal=true — the REAL url lands on the
            # clipboard even while the row is showing the masked display value
            page.locator("#react-conninfo-copy").click()
            page.wait_for_function(
                "navigator.clipboard.readText().then(t => t === "
                + json.dumps(TEST_DB_URL)
                + ")"
            )
        finally:
            ctx.close()


def test_react_conninfo_offers_create_local_when_set_has_none(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-conninfo-btn").click()
            page.wait_for_selector('[data-testid="conninfo-url"]')
            assert page.locator("#react-conninfo-mklocal").count() == 1
            assert page.locator("#react-conninfo-sync").count() == 0
        finally:
            ctx.close()


LOCAL_ENV_TOML = f"""
[shoploc_dev]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "dev"
db = "shoploc"

[shoploc_local]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "local"
db = "shoploc"
"""


def test_react_conninfo_offers_sync_on_local_env(_pw_browser, tmp_path):
    with _running_gui(tmp_path, extra_conn=LOCAL_ENV_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator('[data-testid="conn-row"][data-db="shoploc"]').click()
            page.locator('[data-testid="env-pills"] .env-pill[data-env="local"]').click()
            page.wait_for_function("document.querySelector('.run-target').textContent.includes('local')")
            page.locator("#react-conninfo-btn").click()
            page.wait_for_selector('[data-testid="conninfo-url"]')
            assert page.locator("#react-conninfo-sync").count() == 1
            assert page.locator("#react-conninfo-mklocal").count() == 0
        finally:
            ctx.close()


def test_react_workspace_manager_add_flags_missing_and_remove(_pw_browser, tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-ws-btn").click()
            page.wait_for_selector(".ws-add")
            assert "No workspaces registered" in page.locator("#react-workspace-modal").inner_text()

            other_ws = str(tmp_path / "other_ws")  # never created on disk on purpose
            page.fill("#react-workspace-input", other_ws)
            page.locator("#react-workspace-add-btn").click()
            page.wait_for_selector('[data-testid="ws-row"]')
            row = page.locator('[data-testid="ws-row"]')
            assert other_ws in row.inner_text()
            assert "missing" in row.inner_text()

            page.once("dialog", lambda d: d.accept())
            page.locator('[data-testid="ws-remove"]').click()
            page.wait_for_selector('[data-testid="ws-row"]', state="detached")
            assert "No workspaces registered" in page.locator("#react-workspace-modal").inner_text()

            page.mouse.click(5, 5)  # click outside closes, same as every other modal
            assert page.locator("#react-workspace-modal").count() == 0
        finally:
            ctx.close()


def test_react_workspace_manager_add_and_remove_refreshes_connections_live(_pw_browser, tmp_path, monkeypatch):
    """A workspace add/remove must refresh the sidebar/header connection set
    immediately (mirrors the legacy GUI's renderWorkspaces() -> loadSide()),
    not just the modal's own workspace list.

    Only reachable from a GUI session with no explicit --workspace pin (an
    explicit pin takes full precedence over config.toml by design, same as
    the legacy test's setup), so this switches to a config.toml-driven
    session — still keeping testpg visible — right after the server starts."""
    from quarry import workspace

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    extra_ws = tmp_path / "extra_ws"
    extra_ws.mkdir()
    (extra_ws / "connections.toml").write_text(
        '[extradb]\nurl = "postgresql://localhost:5432/does_not_matter"\nengine = "postgres"\nenv = "test"\n',
        encoding="utf-8",
    )
    with _running_gui(tmp_path) as base:
        workspace._write_config_workspaces([str(tmp_path)])
        workspace.configure_workspace(None)
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            assert page.locator('[data-testid="conn-row"][data-db="extradb"]').count() == 0

            page.locator("#react-ws-btn").click()
            page.wait_for_selector(".ws-add")
            page.fill("#react-workspace-input", str(extra_ws))
            page.locator("#react-workspace-add-btn").click()
            extra_row = page.locator(f'[data-testid="ws-row"][data-dir="{extra_ws}"]')
            extra_row.wait_for(state="visible")

            # the new workspace's connection appears in the sidebar without a
            # page reload
            page.wait_for_selector('[data-testid="conn-row"][data-db="extradb"]')

            page.once("dialog", lambda d: d.accept())
            extra_row.locator('[data-testid="ws-remove"]').click()
            extra_row.wait_for(state="detached")

            # and disappears again once the workspace is removed
            page.wait_for_selector('[data-testid="conn-row"][data-db="extradb"]', state="detached")
        finally:
            ctx.close()


def test_react_workspace_manager_remove_unbinds_active_connection_immediately(_pw_browser, tmp_path, monkeypatch):
    """Removing the workspace behind the currently selected connection must
    unbind it right away — no stale Run/EXPLAIN/conn-info affordances left
    pointing at a connection that no longer resolves. Same config.toml-driven
    (non-explicit) session as the sibling live-refresh test above."""
    from quarry import workspace

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    extra_ws = tmp_path / "extra_ws"
    extra_ws.mkdir()
    (extra_ws / "connections.toml").write_text(
        '[extradb]\nurl = "postgresql://localhost:5432/does_not_matter"\nengine = "postgres"\nenv = "test"\n',
        encoding="utf-8",
    )
    with _running_gui(tmp_path) as base:
        workspace._write_config_workspaces([str(tmp_path)])
        workspace.configure_workspace(None)
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-ws-btn").click()
            page.wait_for_selector(".ws-add")
            page.fill("#react-workspace-input", str(extra_ws))
            page.locator("#react-workspace-add-btn").click()
            extra_row = page.locator(f'[data-testid="ws-row"][data-dir="{extra_ws}"]')
            extra_row.wait_for(state="visible")
            page.mouse.click(5, 5)  # close the modal, back to the main view

            page.locator('[data-testid="conn-row"][data-db="extradb"]').click()
            page.wait_for_selector("#react-conninfo-btn")  # only rendered while `current` resolves

            page.locator("#react-ws-btn").click()
            extra_row = page.locator(f'[data-testid="ws-row"][data-dir="{extra_ws}"]')
            extra_row.wait_for(state="visible")
            page.once("dialog", lambda d: d.accept())
            extra_row.locator('[data-testid="ws-remove"]').click()
            extra_row.wait_for(state="detached")
            page.mouse.click(5, 5)

            page.wait_for_selector("#react-conninfo-btn", state="detached")
        finally:
            ctx.close()


def test_react_format_button_uppercases_and_newlines(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.fill("#react-sql-input", "select   *   from customers")
            page.locator("#react-format-btn").click()
            after = page.locator("#react-sql-input").input_value()
            assert "SELECT" in after
            assert "\nFROM" in after
        finally:
            ctx.close()


def test_react_explain_opens_plan_modal_and_escape_closes(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.fill("#react-sql-input", "select * from customers")
            page.locator("#react-explain-btn").click()
            page.wait_for_selector("#react-modal", timeout=15000)
            mtxt = page.locator("#react-modal").inner_text()
            assert "EXPLAIN" in mtxt
            assert "cost=" in mtxt or "Scan" in mtxt
            page.keyboard.press("Escape")
            page.wait_for_selector("#react-modal-backdrop", state="detached")
        finally:
            ctx.close()


def test_react_explain_redis_toast(_pw_browser, tmp_path):
    with _redis_running() as rurl:
        extra = f'\n[testredis]\nurl = "{rurl}"\nengine = "redis"\n'
        with _running_gui(tmp_path, extra_conn=extra) as base:
            ctx, page = _open_react_page(_pw_browser, base)
            try:
                page.click('[data-testid="conn-row"][data-db="testredis"]')
                page.fill("#react-sql-input", "get foo")
                page.locator("#react-explain-btn").click()
                page.wait_for_selector("#react-toast", state="visible")
                assert "redis" in page.locator("#react-toast").inner_text().lower()
                assert page.locator("#react-modal-backdrop").count() == 0
            finally:
                ctx.close()


def test_react_explain_suppressed_when_tab_switched_mid_flight(_pw_browser, tmp_path):
    """Mirrors the connection-isolation invariant (issue #51/#18): an EXPLAIN
    request fired from tab A must not pop its plan modal if the user has since
    switched away to tab B before the response lands."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.add_init_script(_DELAY_QUERY_INIT_SCRIPT_TMPL.format(marker="explain_slow_marker"))
            page.fill("#react-sql-input", "select 1 as explain_slow_marker")
            page.locator("#react-explain-btn").click()
            page.wait_for_function(
                "document.querySelector('#react-explain-btn').disabled === true"
            )

            page.locator("#react-tab-add").click()  # switch to a fresh tab B before it resolves
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")

            page.wait_for_timeout(900)  # let tab A's slow EXPLAIN response land
            assert page.locator("#react-modal-backdrop").count() == 0
        finally:
            ctx.close()


def test_react_history_modal_empty_state(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.locator("#react-history-btn").click()
            page.wait_for_selector('[data-testid="history-empty"]')
            assert page.locator('[data-testid="history-empty"]').inner_text() == "No history yet"
        finally:
            ctx.close()


def test_react_history_modal_search_filters_and_shows_relative_time(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            _run_react_sql(page, "select 11 as aa_marker")
            _run_react_sql(page, "select 22 as bb_marker")
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-modal .hist-item")
            assert page.locator("#react-history-modal .hist-item").count() == 2
            page.fill('[data-testid="history-search"]', "aa_marker")
            page.wait_for_function(
                "document.querySelectorAll('#react-history-modal .hist-item').length === 1"
            )
            item = page.locator("#react-history-modal .hist-item")
            assert "aa_marker" in item.inner_text()
            assert "ago" in item.locator(".hist-meta").inner_text() or "just now" in item.locator(
                ".hist-meta"
            ).inner_text()

            item.click()  # recall closes the modal and restores the SQL
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value.includes('aa_marker')"
            )
            assert page.locator("#react-history-modal").count() == 0
        finally:
            ctx.close()


def test_react_max_rows_selector_persists_across_reload(_pw_browser, tmp_path):
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.select_option("#react-max-rows", "100")
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            assert page.locator("#react-max-rows").input_value() == "100"
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# issue #53: every legacy `/` GUI localStorage key has a one-time migration
# path into the React store's own `qy_react_*` keys, exercised the first time
# the new key has never been written. Mirrors the equivalent legacy-key
# coverage in test_gui_browser_features.py, adapted to the React shell.
# ---------------------------------------------------------------------------


def test_react_legacy_scalar_prefs_migrate_on_first_load(_pw_browser, tmp_path):
    """qy_lang/qy_theme/qy_sw/qy_edh/qy_maxrows migrate into uiStore the first
    time its own qy_react_* keys have never been written — and, per #53
    review r1-1, that migration actually converges: the values get written
    back into their own qy_react_* keys immediately, not just re-derived by
    re-reading the legacy keys on every load."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.evaluate(
                "()=>{"
                "localStorage.setItem('qy_lang','zh');"
                "localStorage.setItem('qy_theme','dark');"
                "localStorage.setItem('qy_sw','340');"
                "localStorage.setItem('qy_edh','220');"
                "localStorage.setItem('qy_maxrows','2000');"
                "}"
            )
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            assert page.locator("#react-lang-btn").inner_text() == "EN"  # already zh -> toggle offers EN
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            assert abs(page.evaluate("document.querySelector('.conn-side').offsetWidth") - 340) <= 2
            assert abs(page.evaluate("document.querySelector('.edwrap').offsetHeight") - 220) <= 2
            assert page.locator("#react-max-rows").input_value() == "2000"

            assert page.evaluate("localStorage.getItem('qy_react_lang')") == "zh"
            assert page.evaluate("localStorage.getItem('qy_react_theme')") == "dark"
            assert page.evaluate("localStorage.getItem('qy_react_sw')") == "340"
            assert page.evaluate("localStorage.getItem('qy_react_edh')") == "220"
            assert page.evaluate("localStorage.getItem('qy_react_maxrows')") == "2000"

            # A later edit made only to the legacy `/` GUI's keys must no
            # longer leak into `/app` — it now owns its own converged state.
            page.evaluate("()=>{localStorage.setItem('qy_theme','light');}")
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        finally:
            ctx.close()


def test_react_legacy_collapsed_groups_migrate_on_first_load(_pw_browser, tmp_path):
    """qy_collapsed migrates into uiStore's collapsedGroups the first time
    qy_react_collapsed has never been written. The group key format
    (`${ws}::${group}`) is read straight off the toggle's data-group attribute
    rather than hardcoded, since it embeds this run's tmp workspace path."""
    with _running_gui(tmp_path, extra_conn=GROUPED_TOML) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="conn-row"][data-db="shopgrp"]')
            gkey = page.locator('[data-testid="conn-group-toggle"]', has_text="acme").get_attribute(
                "data-group"
            )
            page.evaluate("(gkey)=>{localStorage.setItem('qy_collapsed',JSON.stringify([gkey]));}", gkey)
            page.reload(wait_until="networkidle")
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"]')
            assert page.locator('[data-testid="conn-row"][data-db="shopgrp"]').count() == 0
            assert page.evaluate("localStorage.getItem('qy_react_collapsed')") == f'["{gkey}"]'
        finally:
            ctx.close()


def test_react_legacy_collapsed_ungrouped_key_migrates_on_first_load(_pw_browser, tmp_path):
    """#53 review r1-2: the legacy `/` GUI keys an UNGROUPED connection's
    collapse state by its localized "other"/"其他" label (`${ws}::other`),
    not an empty group name — React's own groupKey() always uses `${ws}::`
    regardless of language, so that legacy key must be normalized on
    migration or an ungrouped group collapsed under the old GUI would never
    come back collapsed under `/app`."""
    with _running_gui(tmp_path) as base:  # default connection (testpg) has no group
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.wait_for_selector('[data-testid="conn-row"][data-db="testpg"]')
            gkey = page.locator('[data-testid="conn-group-toggle"]').get_attribute("data-group")
            assert gkey.endswith("::")  # React's own ungrouped-bucket key has no label
            legacy_key = gkey[:-2] + "::other"
            page.evaluate(
                "(k)=>{localStorage.setItem('qy_collapsed',JSON.stringify([k]));}", legacy_key
            )
            page.reload(wait_until="networkidle")
            page.wait_for_selector('[data-testid="conn-tree"]')
            assert page.locator('[data-testid="conn-row"][data-db="testpg"]').count() == 0
            assert page.evaluate("localStorage.getItem('qy_react_collapsed')") == f'["{gkey}"]'
        finally:
            ctx.close()


def test_react_legacy_history_migrates_on_first_load(_pw_browser, tmp_path):
    """qy_hist migrates into useSqlHistory's qy_react_hist the first time the
    latter has never been written, and the migration converges (#53 review
    r1-1): the entries get written back into qy_react_hist immediately."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.evaluate(
                "()=>{localStorage.setItem('qy_hist',JSON.stringify(["
                "{sql:'select 999 as legacy_hist_marker',db:'testpg',env:'test',ts:Date.now()}"
                "]));}"
            )
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-modal .hist-item")
            assert (
                page.locator("#react-history-modal .hist-item", has_text="legacy_hist_marker").count() == 1
            )
            own = page.evaluate("JSON.parse(localStorage.getItem('qy_react_hist'))")
            assert own and own[0]["sql"] == "select 999 as legacy_hist_marker"
        finally:
            ctx.close()


def test_react_legacy_history_bare_string_entries_migrate(_pw_browser, tmp_path):
    """#53 review r1-3: an even older qy_hist format stores bare SQL strings
    instead of `{sql,db,env,ts}` objects (see gui.py's `hSql` helper). Those
    must be normalized on migration — otherwise `.sql` is `undefined` and the
    History modal's search crashes on the first keystroke."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page(_pw_browser, base)
        try:
            page.evaluate(
                "()=>{localStorage.setItem('qy_hist',JSON.stringify("
                "['select 777 as bare_hist_marker']));}"
            )
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#react-sql-input")
            page.locator("#react-history-btn").click()
            page.wait_for_selector("#react-history-modal .hist-item")
            assert (
                page.locator("#react-history-modal .hist-item", has_text="bare_hist_marker").count() == 1
            )
            page.fill('[data-testid="history-search"]', "bare_hist_marker")  # must not throw
            page.wait_for_function(
                "document.querySelectorAll('#react-history-modal .hist-item').length === 1"
            )
        finally:
            ctx.close()


def _open_react_page_seeded(browser, base, seed_script):
    """Like _open_react_page, but `seed_script` (raw localStorage writes) runs
    via add_init_script BEFORE the app's first mount. Required for legacy tab
    migration: the app's own default-connection-select effect persists a
    blank tab into qy_react_tabs almost immediately on mount, which would
    otherwise win the race against a plain evaluate()-after-load + reload."""
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    page = ctx.new_page()
    page.add_init_script(seed_script)
    page.goto(f"{base}/app/", wait_until="networkidle")
    page.wait_for_selector("#react-sql-input", state="visible")
    return ctx, page


def test_react_legacy_qy_ui_migrates_into_tabs(_pw_browser, tmp_path):
    """The even-older single-tab qy_ui key (predates qy_tabs) migrates into a
    tab when qy_tabs itself was never written either."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page_seeded(
            _pw_browser,
            base,
            "localStorage.setItem('qy_ui',JSON.stringify("
            "{sql:'select 1 as legacy_ui_marker',db:'testpg',env:'test'}));",
        )
        try:
            page.wait_for_function(
                "document.querySelector('#react-sql-input').value === 'select 1 as legacy_ui_marker'"
            )
        finally:
            ctx.close()


def test_react_legacy_qy_tabs_migrates_on_first_load(_pw_browser, tmp_path):
    """qy_tabs (+ qy_ati for the active index, + per-tab titles) migrates into
    qy_react_tabs the first time the latter has never been written."""
    with _running_gui(tmp_path) as base:
        ctx, page = _open_react_page_seeded(
            _pw_browser,
            base,
            "localStorage.setItem('qy_tabs',JSON.stringify(["
            "{sql:'select 1 as t1',db:'testpg',env:'test',title:null},"
            "{sql:'select 2 as t2',db:'testpg',env:'test',title:'My tab'}"
            "]));"
            "localStorage.setItem('qy_ati','1');",
        )
        try:
            page.wait_for_function("document.querySelectorAll('#react-tabs [data-testid=tab]').length === 2")
            assert page.input_value("#react-sql-input") == "select 2 as t2"  # qy_ati=1 carried over
            assert page.locator("#react-tabs [data-testid=tab] .lbl").nth(1).inner_text() == "My tab"
        finally:
            ctx.close()


_REACT_LEGACY_RES = (
    "{columns:[{name:'v',type:'int4'}],rows:[{v:42}],rowCount:1,elapsedMs:1,engine:'postgres'}"
)


def _open_react_page_with_legacy_result(browser, base, tab_env):
    """Opens a fresh /app/ page with qy_tabs (single tab bound to shop@{tab_env})
    and qy_result (a legacy single-result payload produced by shop@dev) seeded
    before mount — mirrors test_gui_browser_features.py's _seed_legacy for the
    React tabsStore."""
    return _open_react_page_seeded(
        browser,
        base,
        "localStorage.setItem('qy_tabs',JSON.stringify("
        f"[{{sql:'select 42 as v',db:'shop',env:'{tab_env}'}}]));"
        "localStorage.setItem('qy_ati','0');"
        f"localStorage.setItem('qy_result',JSON.stringify({{db:'shop',env:'dev',res:{_REACT_LEGACY_RES}}}));",
    )


def test_react_legacy_qy_result_env_mismatch_not_restored(_pw_browser, tmp_path):
    """qy_result -> per-tab result migration validates BOTH db AND env: a tab
    re-pointed to a different env than the one that produced the legacy
    result must never come back showing that stale grid."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page_with_legacy_result(_pw_browser, base, "prod")  # legacy result env=dev
        try:
            assert page.locator('#react-grid td[data-v="42"]').count() == 0
        finally:
            ctx.close()


def test_react_legacy_qy_result_env_match_restored(_pw_browser, tmp_path):
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page_with_legacy_result(_pw_browser, base, "dev")  # tab env matches
        try:
            page.wait_for_selector('#react-grid td[data-v="42"]')
        finally:
            ctx.close()


def test_react_legacy_qy_tabres_migrates_on_first_load(_pw_browser, tmp_path):
    """qy_tabres (the newer, index-aligned-array legacy result format, tried
    before the older single-result qy_result) migrates into qy_react_tabres
    the first time the latter has never been written."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as base:
        ctx, page = _open_react_page_seeded(
            _pw_browser,
            base,
            "localStorage.setItem('qy_tabs',JSON.stringify("
            "[{sql:'select 42 as v',db:'shop',env:'dev'}]));"
            "localStorage.setItem('qy_ati','0');"
            "localStorage.setItem('qy_tabres',JSON.stringify("
            f"[{{db:'shop',env:'dev',res:{_REACT_LEGACY_RES}}}]));",
        )
        try:
            page.wait_for_selector('#react-grid td[data-v="42"]')
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
