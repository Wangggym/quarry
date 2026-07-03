# Agent rules for this repo

Guidance for AI coding agents (and humans) working on Quarry.

## GUI changes (`src/quarry/gui.py`)

The entire GUI is one file: ~440 lines of Python plus ~800 lines of JS inside
the `INDEX_HTML` string. Because of that, its feature surface is *exhaustively
enumerable* — and we keep it enumerated:

1. **The GUI feature matrix in [TESTING.md](TESTING.md) is the source of truth
   for frontend coverage.** Any change that adds, alters, or removes a
   user-visible GUI behavior must update the matching matrix row(s) in the same
   PR — including honest status downgrades (✅→🟡/❌) when behavior changed and
   the old test no longer proves it.
2. **New feature points get a browser test** in
   `tests/test_gui_browser_features.py` (page fixtures come from
   `tests/conftest.py`; console errors are an autouse invariant in that module).
   Backend/API logic belongs in `test_gui_backend.py` / `test_gui_api.py` — do
   not re-test engine behavior through the browser.
3. **Run all three audits after editing `INDEX_HTML`** (details + state
   inventory in TESTING.md "Keeping the matrix honest"):
   - *Existence*: every interaction binding, localStorage key, and consumed
     `/api/*` endpoint maps to a matrix row (catches untested code);
   - *Capability*: per UI region, list what a SQL-client user would expect,
     diff against reality, file misses in the Design-gaps table (catches
     missing features — existence auditing can never see these);
   - *Shared-state*: when writing any global state (`cur`, `lastRes`,
     `TABS/TABRES`, `HIST`, `sortState`, `TCACHE`, …), check every reader;
     features sharing state need an interaction test (catches cross-feature
     bugs like "export uses another tab's result").
4. **When adding a UX invariant, enumerate ALL write sites** of the protected
   state (grep, list them in the PR) and cover each — partial rollout of an
   invariant is how "closing a tab loses SQL" shipped.
5. **UX invariants that must never regress** (each is pinned by a browser test):
   - hand-written editor SQL is never silently lost — every editor overwrite
     (table click / key inspect / saved query / history recall / tab close)
     goes through `keepDraft()`/`pushHist()`; Cmd/Ctrl+↓ restores the stash;
   - switching the env pill to **prod never auto-runs** the current SQL;
   - overlapping query responses are **latest-wins** (`runSeq` guard) — a stale
     response must never repaint the grid;
   - the grid, status bar, and exports always reflect the **active tab's**
     result (`TABRES`), never another tab's;
   - column sort is numeric-aware, and a third click restores original order;
   - lists are never silently truncated (redis 400-key cap, 5000-table cap →
     visible notice).

## Output & docs

- User-visible changes get a `CHANGELOG.md` entry under `[Unreleased]`.
- External output (PR titles/bodies, commit messages) carries business-relevant
  content only — no internal debugging-tool narration.
