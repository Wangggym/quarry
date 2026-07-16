"""Visual-parity pins for the React GUI (epic v2 requirement): the design
tokens must resolve to EXACTLY the retired embedded GUI's "Slate & Copper"
hex values, in both themes, on real rendered elements — asserted via
getComputedStyle so a repaint/regression is caught by CI, not by eyeballing
screenshots. Values below are the legacy INDEX_HTML <style> hexes verbatim.
"""

from __future__ import annotations

import pytest

from conftest import requires_browser

pytestmark = [requires_browser, pytest.mark.browser]


def _style(page, selector: str, prop: str) -> str:
    return page.evaluate(
        "([sel, prop]) => getComputedStyle(document.querySelector(sel))[prop]",
        [selector, prop],
    )


# legacy hexes -> the rgb() strings getComputedStyle returns
DARK = {
    "bg0": "rgb(14, 17, 22)",      # #0e1116
    "bg1": "rgb(28, 32, 40)",      # #1c2028
    "fg": "rgb(238, 240, 243)",    # #eef0f3
    "accent": "rgb(192, 130, 79)", # #c0824f
    "accent_ink": "rgb(36, 20, 7)",# #241407
    "ok": "rgb(143, 180, 140)",    # #8fb48c
    "ok_bg": "rgb(35, 47, 36)",    # #232f24
}
LIGHT = {
    "bg0": "rgb(244, 243, 238)",   # #f4f3ee
    "bg1": "rgb(255, 255, 255)",   # #ffffff
    "fg": "rgb(28, 28, 25)",       # #1c1c19
    "accent": "rgb(176, 106, 52)", # #b06a34
    "accent_ink": "rgb(255, 255, 255)",  # #ffffff
}


def test_default_theme_is_dark_with_legacy_palette(page):
    assert page.evaluate("document.documentElement.dataset.mode") == "dark"
    assert _style(page, "body", "backgroundColor") == DARK["bg0"]
    assert _style(page, "body", "color") == DARK["fg"]
    assert _style(page, "header", "backgroundColor") == DARK["bg1"]
    # primary Run button carries the copper accent + its ink color
    assert _style(page, "#runBtn", "backgroundColor") == DARK["accent"]
    assert _style(page, "#runBtn", "color") == DARK["accent_ink"]
    # read-only badge: ok-green on ok-bg
    assert _style(page, "#roBadge", "color") == DARK["ok"]
    assert _style(page, "#roBadge", "backgroundColor") == DARK["ok_bg"]


def test_light_theme_matches_legacy_palette(page):
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"
    assert _style(page, "body", "backgroundColor") == LIGHT["bg0"]
    assert _style(page, "body", "color") == LIGHT["fg"]
    assert _style(page, "header", "backgroundColor") == LIGHT["bg1"]
    assert _style(page, "#runBtn", "backgroundColor") == LIGHT["accent"]
    assert _style(page, "#runBtn", "color") == LIGHT["accent_ink"]


def test_typography_matches_legacy(page):
    # app-wide 14px sans stack; the editor runs on the mono stack
    assert _style(page, "body", "fontSize") == "14px"
    assert "-apple-system" in _style(page, "body", "fontFamily")
    sql_font = _style(page, "#sql", "fontFamily")
    assert "Menlo" in sql_font or "monospace" in sql_font


def test_icons_use_selfhosted_tabler_font(page):
    # icon glyphs render through the vendored tabler-icons webfont (no CDN)
    fam = _style(page, "#healthBtn .ti", "fontFamily")
    assert "tabler-icons" in fam
    loaded = page.evaluate("document.fonts.check('16px tabler-icons')")
    assert loaded, "tabler-icons webfont did not load"
