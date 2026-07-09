"""Browser + API checks for the React scaffold at /app (issue #21).

The legacy INDEX_HTML GUI at / is unchanged; this file only covers the new
strangler-fig shell served from web_dist.
"""

from __future__ import annotations

import re
import urllib.request
import zipfile
from pathlib import Path

import pytest

from conftest import REPO, _running_gui, requires_browser
from quarry import __version__

pytestmark = [requires_browser, pytest.mark.browser]


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
