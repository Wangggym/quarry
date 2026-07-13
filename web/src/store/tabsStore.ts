import { create } from "zustand";
import type { QueryResult } from "../api";
import { t } from "../i18n";

export type TabId = string;

/** A single editor tab: its own SQL draft and the connection it targets.
 * `title` is a user-set rename; `null` falls back to the automatic
 * `db@env` / first-SQL-words title (see `tabTitle`). */
export type Tab = {
  id: TabId;
  title: string | null;
  sql: string;
  db: string | null;
  env: string | null;
};

/** Per-tab result snapshot, tagged with the connection that PRODUCED the
 * result (`queryDb`/`queryEnv`) — not necessarily the tab's current one. A
 * tab re-pointed to another connection must never have its grid repainted
 * with a mismatched result. `querySql` is the exact SQL that produced the
 * page (set by Run only) — it is what makes "load more" possible. */
export type TabResultSnapshot = {
  result: QueryResult | null;
  queryDb: string | null;
  queryEnv: string | null;
  querySql: string | null;
};

// Exactly the legacy GUI's storage keys AND value formats (qy_tabs is a flat
// array of {id:'t3',sql,db,env,title?}; qy_ati an index; qy_tabres an
// index-aligned array of {db,env,res}) — an existing user's tabs, results and
// active tab carry over unchanged, and the browser suite's storage-shape
// assertions apply as-is.
const TABS_KEY = "qy_tabs";
const ATI_KEY = "qy_ati";
const TABRES_KEY = "qy_tabres";
// Even older single-state keys, migrated from when qy_tabs was never written.
const LEGACY_UI_KEY = "qy_ui";
const LEGACY_RESULT_KEY = "qy_result";

// Tab ids are 't<n>' with a monotonically increasing counter seeded past
// every persisted id, so ids are never reused across reloads (an in-flight
// request routed by id must not land on an unrelated new tab).
let TID = 0;

function newId(): TabId {
  return `t${++TID}`;
}

type StoredTab = { id?: string; sql?: string; db?: string | null; env?: string | null; title?: string | null };
type StoredResult = { db?: string | null; env?: string | null; res?: (QueryResult & { _sql?: string }) | null } | null;

function blankTab(seed?: { db?: string | null; env?: string | null }): Tab {
  return { id: newId(), title: null, sql: "", db: seed?.db ?? null, env: seed?.env ?? null };
}

function readStoredTabs(): StoredTab[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(TABS_KEY) || "null");
    if (Array.isArray(parsed) && parsed.length > 0) return parsed;
  } catch {
    // corrupt value — fall through to the single-state key
  }
  try {
    const ui = JSON.parse(localStorage.getItem(LEGACY_UI_KEY) || "null") as StoredTab | null;
    return [{ sql: ui?.sql ?? "", db: ui?.db ?? null, env: ui?.env ?? null }];
  } catch {
    return [{ sql: "", db: null, env: null }];
  }
}

/** Restore each tab's persisted result, but ONLY when its producing
 * connection still matches that tab's current db/env — both fields, not just
 * db. Falls back to the pre-tabres single-result key for the active tab. */
function readStoredResults(tabs: Tab[], ati: number): Record<TabId, TabResultSnapshot> {
  const out: Record<TabId, TabResultSnapshot> = {};
  const accept = (entry: StoredResult, tab: Tab): void => {
    if (!entry?.res) return;
    if (entry.db !== tab.db || (entry.env ?? null) !== (tab.env ?? null)) return;
    const { _sql, ...result } = entry.res;
    out[tab.id] = {
      result: result as QueryResult,
      queryDb: entry.db ?? null,
      queryEnv: entry.env ?? null,
      querySql: _sql ?? null,
    };
  };
  try {
    const parsed = JSON.parse(localStorage.getItem(TABRES_KEY) || "null");
    if (Array.isArray(parsed)) {
      tabs.forEach((tab, i) => accept(parsed[i] as StoredResult, tab));
      return out;
    }
  } catch {
    // corrupt value — fall through to the single-result key
  }
  try {
    const single = JSON.parse(localStorage.getItem(LEGACY_RESULT_KEY) || "null") as StoredResult;
    const tab = tabs[ati];
    if (tab) accept(single, tab);
  } catch {
    // corrupt value — start with no restored results
  }
  return out;
}

function readInitial(): { tabs: Tab[]; activeId: TabId; results: Record<TabId, TabResultSnapshot> } {
  const stored = readStoredTabs();
  for (const tb of stored) {
    const n = Number(String(tb.id || "").slice(1));
    if (n > TID) TID = n;
  }
  const tabs: Tab[] = stored.map((tb) => ({
    id: tb.id || newId(),
    title: tb.title ?? null,
    sql: tb.sql ?? "",
    db: tb.db ?? null,
    env: tb.env ?? null,
  }));
  const ati = Math.min(Math.max(Number(localStorage.getItem(ATI_KEY) || 0) || 0, 0), tabs.length - 1);
  return { tabs, activeId: tabs[ati].id, results: readStoredResults(tabs, ati) };
}

function persistTabs(tabs: Tab[], activeId: TabId): void {
  try {
    localStorage.setItem(
      TABS_KEY,
      JSON.stringify(tabs.map((tb) => ({ id: tb.id, sql: tb.sql, db: tb.db, env: tb.env, title: tb.title }))),
    );
    localStorage.setItem(ATI_KEY, String(Math.max(tabs.findIndex((t) => t.id === activeId), 0)));
  } catch {
    // storage full/unavailable — tabs just won't survive a reload
  }
}

/** Persist every tab's result index-aligned with qy_tabs, tagged with the
 * producing connection. On quota overflow, degrade to keeping only the
 * active tab's result (the legacy fallback). */
function persistResults(tabs: Tab[], activeId: TabId, results: Record<TabId, TabResultSnapshot>): void {
  const pack = (tab: Tab): StoredResult => {
    const snap = results[tab.id];
    if (!snap?.result) return null;
    const res = snap.querySql ? { ...snap.result, _sql: snap.querySql } : snap.result;
    return { db: snap.queryDb, env: snap.queryEnv, res };
  };
  try {
    localStorage.setItem(TABRES_KEY, JSON.stringify(tabs.map(pack)));
  } catch {
    try {
      const arr: StoredResult[] = tabs.map(() => null);
      const ati = tabs.findIndex((t) => t.id === activeId);
      if (ati >= 0) arr[ati] = pack(tabs[ati]);
      localStorage.setItem(TABRES_KEY, JSON.stringify(arr));
    } catch {
      try {
        localStorage.removeItem(TABRES_KEY);
      } catch {
        // storage completely unavailable — results just won't survive a reload
      }
    }
  }
}

/** Extracts the single main table an SQL statement targets — the identifier
 * following `FROM`/`UPDATE`/`INTO` (covers SELECT, DELETE FROM, UPDATE and
 * INSERT INTO). Returns null when no such keyword is found, or the
 * statement joins multiple tables, since there is then no single table to
 * summarize a title by (callers fall back to raw SQL words in that case). */
export function parseMainTable(sql: string): string | null {
  const cleaned = sql.replace(/--.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
  if (/\bjoin\b/i.test(cleaned)) return null;
  const m = /\b(?:from|update|into)\s+([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?)/i.exec(cleaned);
  return m ? m[1] : null;
}

/** A user rename always wins. Else, a non-empty SQL body is what
 * distinguishes tabs on the same connection: prefer the main table it
 * targets (`parseMainTable`), falling back to its first two words when no
 * single table can be parsed out (multi-table JOIN, non-DML statements…).
 * An empty SQL body falls back to `db@env`, then the localized "new query". */
export function tabTitle(tab: Tab): string {
  if (tab.title) return tab.title;
  const sql = tab.sql.trim();
  if (sql) return parseMainTable(sql) ?? sql.split(/\s+/).slice(0, 2).join(" ");
  if (tab.db) return tab.db + (tab.env ? `@${tab.env}` : "");
  return t("new_query");
}

export type TabsState = {
  tabs: Tab[];
  activeId: TabId;
  /** Keyed by tab id (stable across reorder/close); persisted index-aligned. */
  results: Record<TabId, TabResultSnapshot>;
  addTab: (seed?: { db?: string | null; env?: string | null }) => void;
  switchTab: (id: TabId) => void;
  closeTab: (id: TabId) => void;
  renameTab: (id: TabId, title: string | null) => void;
  reorderTab: (fromId: TabId, toId: TabId) => void;
  updateActiveTab: (patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
  updateTab: (id: TabId, patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
  setTabResult: (id: TabId, snapshot: TabResultSnapshot | null) => void;
};

export const useTabsStore = create<TabsState>((set, get) => {
  const initial = readInitial();
  const save = (tabs: Tab[], activeId: TabId, results: Record<TabId, TabResultSnapshot>): void => {
    persistTabs(tabs, activeId);
    persistResults(tabs, activeId, results);
  };
  return {
    tabs: initial.tabs,
    activeId: initial.activeId,
    results: initial.results,

    addTab: (seed) => {
      const s = get();
      const active = s.tabs.find((t) => t.id === s.activeId);
      const tab = blankTab(seed ?? { db: active?.db ?? null, env: active?.env ?? null });
      const tabs = [...s.tabs, tab];
      save(tabs, tab.id, s.results);
      set({ tabs, activeId: tab.id });
    },

    switchTab: (id) => {
      const s = get();
      if (!s.tabs.some((t) => t.id === id) || id === s.activeId) return;
      save(s.tabs, id, s.results);
      set({ activeId: id });
    },

    closeTab: (id) => {
      const s = get();
      const idx = s.tabs.findIndex((t) => t.id === id);
      if (idx === -1) return;
      const active = s.tabs.find((t) => t.id === s.activeId);
      let tabs = s.tabs.filter((t) => t.id !== id);
      if (tabs.length === 0) tabs = [blankTab({ db: active?.db ?? null, env: active?.env ?? null })];
      const activeId = s.activeId === id ? tabs[Math.min(idx, tabs.length - 1)].id : s.activeId;
      const results = { ...s.results };
      delete results[id];
      save(tabs, activeId, results);
      set({ tabs, activeId, results });
    },

    renameTab: (id, title) => {
      const s = get();
      const tabs = s.tabs.map((t) => (t.id === id ? { ...t, title } : t));
      save(tabs, s.activeId, s.results);
      set({ tabs });
    },

    reorderTab: (fromId, toId) => {
      const s = get();
      if (fromId === toId) return;
      const from = s.tabs.findIndex((t) => t.id === fromId);
      const to = s.tabs.findIndex((t) => t.id === toId);
      if (from === -1 || to === -1) return;
      const tabs = [...s.tabs];
      const [moved] = tabs.splice(from, 1);
      tabs.splice(to, 0, moved);
      save(tabs, s.activeId, s.results);
      set({ tabs });
    },

    updateActiveTab: (patch) => {
      get().updateTab(get().activeId, patch);
    },

    updateTab: (id, patch) => {
      const s = get();
      const tabs = s.tabs.map((t) => (t.id === id ? { ...t, ...patch } : t));
      save(tabs, s.activeId, s.results);
      set({ tabs });
    },

    setTabResult: (id, snapshot) => {
      const s = get();
      if (!s.tabs.some((t) => t.id === id)) return; // tab closed while in flight
      const results = { ...s.results };
      if (snapshot) results[id] = snapshot;
      else delete results[id];
      persistResults(s.tabs, s.activeId, results);
      set({ results });
    },
  };
});
