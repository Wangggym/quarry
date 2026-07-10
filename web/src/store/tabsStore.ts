import { create } from "zustand";
import type { Tab, TabId, TabsState } from "./types";

const TABS_KEY = "qy_react_tabs";
// The vanilla `/` GUI's tab list — reused here as a best-effort seed so an
// existing user doesn't land on a blank tab the first time they open `/app`.
const LEGACY_TABS_KEY = "qy_tabs";

function newId(): TabId {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

function blankTab(seed?: { db?: string | null; env?: string | null }): Tab {
  return { id: newId(), title: null, sql: "", db: seed?.db ?? null, env: seed?.env ?? null };
}

type LegacyTab = { sql?: string; db?: string | null; env?: string | null; title?: string | null };

/**
 * Migration *skeleton* only: seeds the new store from the old vanilla GUI's
 * `qy_tabs` if present. It intentionally does not re-validate that a tab's
 * db/env still resolves to a live connection, and it does not restore
 * per-tab results (`qy_tabres`) — the full db+env-aware migration lands in
 * #53 (件7), which also collapses every other localStorage key into this
 * store.
 */
function migrateLegacyTabs(): Tab[] | null {
  try {
    const raw = localStorage.getItem(LEGACY_TABS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length === 0) return null;
    return (parsed as LegacyTab[]).map((tb) => ({
      id: newId(),
      title: tb.title ?? null,
      sql: tb.sql ?? "",
      db: tb.db ?? null,
      env: tb.env ?? null,
    }));
  } catch {
    return null;
  }
}

function readInitial(): { tabs: Tab[]; activeId: TabId } {
  try {
    const raw = localStorage.getItem(TABS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { tabs?: Tab[]; activeId?: TabId };
      if (Array.isArray(parsed.tabs) && parsed.tabs.length > 0) {
        const activeId = parsed.tabs.some((t) => t.id === parsed.activeId)
          ? (parsed.activeId as TabId)
          : parsed.tabs[0].id;
        return { tabs: parsed.tabs, activeId };
      }
    }
  } catch {
    // corrupt value — fall through to migration/blank
  }
  const migrated = migrateLegacyTabs();
  if (migrated) return { tabs: migrated, activeId: migrated[0].id };
  const tab = blankTab();
  return { tabs: [tab], activeId: tab.id };
}

function persist(tabs: Tab[], activeId: TabId): void {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify({ tabs, activeId }));
  } catch {
    // storage full/unavailable — tabs just won't survive a reload
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
      set({ tabs, activeId });
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
      const state = get();
      const tabs = state.tabs.map((t) => (t.id === state.activeId ? { ...t, ...patch } : t));
      persist(tabs, state.activeId);
      set({ tabs });
    },
  };
});
