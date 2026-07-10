import { create } from "zustand";
import type { Tab, TabId, TabResultSnapshot, TabsState } from "./types";

const TABS_KEY = "qy_react_tabs";
// The vanilla `/` GUI's tab list — reused here as a best-effort seed so an
// existing user doesn't land on a blank tab the first time they open `/app`.
const LEGACY_TABS_KEY = "qy_tabs";
// The even OLDER single-tab-state key (predates multi-tab support), used as
// a fallback only when `qy_tabs` itself was never written.
const LEGACY_UI_KEY = "qy_ui";
const LEGACY_ATI_KEY = "qy_ati";
// Per-tab result snapshots (#51, connection isolation), keyed by tab id.
const TABRES_KEY = "qy_react_tabres";
// The vanilla `/` GUI's per-tab result array (index-aligned with `qy_tabs`),
// and the even older single-result key that predates per-tab results.
const LEGACY_TABRES_KEY = "qy_tabres";
const LEGACY_RESULT_KEY = "qy_result";

function newId(): TabId {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

function blankTab(seed?: { db?: string | null; env?: string | null }): Tab {
  return { id: newId(), title: null, sql: "", db: seed?.db ?? null, env: seed?.env ?? null };
}

type LegacyTab = { sql?: string; db?: string | null; env?: string | null; title?: string | null };
type LegacyResultEntry = { db?: string | null; env?: string | null; res?: unknown } | null | undefined;

/**
 * Seeds the new store from the OLD vanilla GUI's tab list (`qy_tabs`), or —
 * if that was never written either — its even older single-tab state
 * (`qy_ui`). Db/env are carried over as-is, unvalidated: a stale connection
 * is caught the same way a live tab's is, via `revalidateTabResult` in
 * ResultWorkbench once a real connection list is available.
 */
function readLegacyTabs(): LegacyTab[] | null {
  try {
    const raw = localStorage.getItem(LEGACY_TABS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed as LegacyTab[];
    }
  } catch {
    // corrupt value — fall through to the single-tab key
  }
  try {
    const raw = localStorage.getItem(LEGACY_UI_KEY);
    if (!raw) return null;
    const ui = JSON.parse(raw) as LegacyTab | null;
    if (!ui) return null;
    return [{ sql: ui.sql ?? "", db: ui.db ?? null, env: ui.env ?? null }];
  } catch {
    return null;
  }
}

/**
 * Restores every legacy tab's persisted result, tagged with the connection
 * that PRODUCED it — checked against BOTH db and env, not just db, so a tab
 * re-pointed since the result was produced never comes back mislabeled
 * (mirrors `readInitialResults` below).
 *
 * Prefers the newer `qy_tabres` (index-aligned with `qy_tabs`); if that's
 * absent, falls back to the even older single-result key `qy_result`,
 * matched against the legacy active-tab index (`qy_ati`).
 */
function migrateLegacyResults(
  migratedTabs: Tab[],
  legacyTabs: LegacyTab[],
): Record<TabId, TabResultSnapshot> {
  const out: Record<TabId, TabResultSnapshot> = {};
  const matches = (entry: LegacyResultEntry, tab: Tab): entry is NonNullable<LegacyResultEntry> =>
    !!entry?.res && entry.db === tab.db && (entry.env ?? null) === (tab.env ?? null);
  const snapshotFor = (entry: NonNullable<LegacyResultEntry>): TabResultSnapshot => ({
    result: entry.res as TabResultSnapshot["result"],
    queryDb: entry.db ?? null,
    queryEnv: entry.env ?? null,
    querySql: null,
  });

  try {
    const raw = localStorage.getItem(LEGACY_TABRES_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as LegacyResultEntry[];
      if (Array.isArray(parsed)) {
        migratedTabs.forEach((tab, i) => {
          const entry = parsed[i];
          if (matches(entry, tab)) out[tab.id] = snapshotFor(entry);
        });
        return out;
      }
    }
  } catch {
    // corrupt value — fall through to the single-result key
  }

  try {
    const raw = localStorage.getItem(LEGACY_RESULT_KEY);
    if (!raw) return out;
    const entry = JSON.parse(raw) as LegacyResultEntry;
    const ati = Math.min(Number(localStorage.getItem(LEGACY_ATI_KEY) ?? 0) || 0, legacyTabs.length - 1);
    const tab = migratedTabs[ati];
    if (tab && matches(entry, tab)) out[tab.id] = snapshotFor(entry);
  } catch {
    // corrupt value — start with no restored results
  }
  return out;
}

function readInitial(): { tabs: Tab[]; activeId: TabId; results: Record<TabId, TabResultSnapshot> } {
  try {
    const raw = localStorage.getItem(TABS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { tabs?: Tab[]; activeId?: TabId };
      if (Array.isArray(parsed.tabs) && parsed.tabs.length > 0) {
        const activeId = parsed.tabs.some((t) => t.id === parsed.activeId)
          ? (parsed.activeId as TabId)
          : parsed.tabs[0].id;
        return { tabs: parsed.tabs, activeId, results: readInitialResults(parsed.tabs) };
      }
    }
  } catch {
    // corrupt value — fall through to migration/blank
  }
  const legacyTabs = readLegacyTabs();
  if (legacyTabs) {
    const migrated = legacyTabs.map((tb) => ({
      id: newId(),
      title: tb.title ?? null,
      sql: tb.sql ?? "",
      db: tb.db ?? null,
      env: tb.env ?? null,
    }));
    const results = migrateLegacyResults(migrated, legacyTabs);
    // The legacy `qy_ati` active-tab index (absent when migrating from the
    // even-older single-tab `qy_ui`, which has no notion of it — clamped to
    // 0 in that case since `migrated` is always length 1).
    const ati = Math.min(
      Math.max(Number(localStorage.getItem(LEGACY_ATI_KEY) ?? 0) || 0, 0),
      migrated.length - 1,
    );
    const activeId = migrated[ati].id;
    // Converge onto the new keys right away so a reload before any further
    // edit doesn't have to re-run this migration from the legacy keys again.
    persist(migrated, activeId);
    persistResults(results);
    return { tabs: migrated, activeId, results };
  }
  const tab = blankTab();
  return { tabs: [tab], activeId: tab.id, results: {} };
}

function persist(tabs: Tab[], activeId: TabId): void {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify({ tabs, activeId }));
  } catch {
    // storage full/unavailable — tabs just won't survive a reload
  }
}

/**
 * Restore persisted per-tab results, but ONLY for entries whose producing
 * connection (`queryDb`/`queryEnv`) still matches that tab's CURRENT db/env
 * — both fields, not just db. A tab re-pointed to another connection (or
 * another env of the same db) since the result was produced must never come
 * back from a reload showing the old connection's grid.
 */
function readInitialResults(tabs: Tab[]): Record<TabId, TabResultSnapshot> {
  const out: Record<TabId, TabResultSnapshot> = {};
  try {
    const raw = localStorage.getItem(TABRES_KEY);
    if (!raw) return out;
    const parsed = JSON.parse(raw) as Record<string, TabResultSnapshot | undefined>;
    for (const tab of tabs) {
      const snap = parsed[tab.id];
      if (!snap || !snap.result) continue;
      if (snap.queryDb === tab.db && (snap.queryEnv ?? null) === (tab.env ?? null)) {
        out[tab.id] = snap;
      }
    }
  } catch {
    // corrupt value — start with no restored results
  }
  return out;
}

function persistResults(results: Record<TabId, TabResultSnapshot>): void {
  try {
    localStorage.setItem(TABRES_KEY, JSON.stringify(results));
  } catch {
    // storage full/unavailable — results just won't survive a reload
  }
}

/** `db@env` when the tab has a connection, else the first two words of its
 * SQL, else "New query" — a user rename always wins. */
export function tabTitle(tab: Tab): string {
  if (tab.title) return tab.title;
  if (tab.db) return tab.db + (tab.env ? `@${tab.env}` : "");
  const words = tab.sql.trim().split(/\s+/).filter(Boolean).slice(0, 2).join(" ");
  return words || "New query";
}

export const useTabsStore = create<TabsState>((set, get) => {
  const initial = readInitial();
  return {
    tabs: initial.tabs,
    activeId: initial.activeId,
    results: initial.results,

    addTab: (seed) => {
      const state = get();
      const active = state.tabs.find((t) => t.id === state.activeId);
      const tab = blankTab(seed ?? { db: active?.db ?? null, env: active?.env ?? null });
      const tabs = [...state.tabs, tab];
      persist(tabs, tab.id);
      set({ tabs, activeId: tab.id });
    },

    switchTab: (id) => {
      const state = get();
      if (!state.tabs.some((t) => t.id === id) || id === state.activeId) return;
      persist(state.tabs, id);
      set({ activeId: id });
    },

    closeTab: (id) => {
      const state = get();
      const idx = state.tabs.findIndex((t) => t.id === id);
      if (idx === -1) return;
      let tabs = state.tabs.filter((t) => t.id !== id);
      if (tabs.length === 0) tabs = [blankTab()];
      const activeId =
        state.activeId === id ? tabs[Math.min(idx, tabs.length - 1)].id : state.activeId;
      persist(tabs, activeId);
      const results = { ...state.results };
      delete results[id];
      persistResults(results);
      set({ tabs, activeId, results });
    },

    renameTab: (id, title) => {
      const state = get();
      const tabs = state.tabs.map((t) => (t.id === id ? { ...t, title } : t));
      persist(tabs, state.activeId);
      set({ tabs });
    },

    reorderTab: (fromId, toId) => {
      const state = get();
      if (fromId === toId) return;
      const from = state.tabs.findIndex((t) => t.id === fromId);
      const to = state.tabs.findIndex((t) => t.id === toId);
      if (from === -1 || to === -1) return;
      const tabs = [...state.tabs];
      const [moved] = tabs.splice(from, 1);
      tabs.splice(to, 0, moved);
      persist(tabs, state.activeId);
      set({ tabs });
    },

    updateActiveTab: (patch) => {
      get().updateTab(get().activeId, patch);
    },

    updateTab: (id, patch) => {
      const state = get();
      const tabs = state.tabs.map((t) => (t.id === id ? { ...t, ...patch } : t));
      persist(tabs, state.activeId);
      set({ tabs });
    },

    setTabResult: (id, snapshot) => {
      const state = get();
      if (!state.tabs.some((t) => t.id === id)) return; // tab closed while in flight
      const results = { ...state.results, [id]: snapshot };
      persistResults(results);
      set({ results });
    },
  };
});
