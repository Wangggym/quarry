import type { QueryResult } from "../api";

export type TabId = string;

/**
 * A single editor tab: its own SQL draft and the connection it targets.
 * `title` is a user-set rename (survives every re-save); when it's `null`
 * the tab bar falls back to the automatic `db@env` / first-SQL-words title
 * (see `tabTitle()` in `./tabsStore`).
 */
export type Tab = {
  id: TabId;
  title: string | null;
  sql: string;
  db: string | null;
  env: string | null;
};

/**
 * Per-tab result-snapshot contract for #51 (件5b, connection isolation).
 * Each entry is tagged with the connection that PRODUCED the result
 * (`queryDb`/`queryEnv`), not necessarily the tab's current one — a tab
 * re-pointed to another connection (or whose in-flight request was
 * superseded) must never have its grid repainted with a mismatched result.
 */
export type TabResultSnapshot = {
  result: QueryResult | null;
  queryDb: string | null;
  queryEnv: string | null;
  querySql: string | null;
};

export type TabsState = {
  tabs: Tab[];
  activeId: TabId;
  /** Keyed by tab id (not index — stable across reorder/close). */
  results: Record<TabId, TabResultSnapshot>;
  /** Seeds the new tab's connection; defaults to the active tab's db/env. */
  addTab: (seed?: { db?: string | null; env?: string | null }) => void;
  switchTab: (id: TabId) => void;
  closeTab: (id: TabId) => void;
  /** `title === null` reverts the tab to its automatic title. */
  renameTab: (id: TabId, title: string | null) => void;
  reorderTab: (fromId: TabId, toId: TabId) => void;
  updateActiveTab: (patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
  updateTab: (id: TabId, patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
  /** Tags the snapshot with the connection that produced it and persists it
   * under `id` — the caller decides whether `id` is still the active tab. */
  setTabResult: (id: TabId, snapshot: TabResultSnapshot) => void;
};
