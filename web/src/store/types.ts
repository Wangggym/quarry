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
 * `ResultWorkbench` still owns a single shared result/loading/status state
 * for this issue (#50) — switching tabs does not yet swap the grid, and
 * in-flight requests are not yet routed back to their origin tab. #51 moves
 * that state here, tagged with the connection that PRODUCED the result (not
 * necessarily the tab's current one), without needing to restructure `Tab`
 * or `TabsState`.
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
  /** Seeds the new tab's connection; defaults to the active tab's db/env. */
  addTab: (seed?: { db?: string | null; env?: string | null }) => void;
  switchTab: (id: TabId) => void;
  closeTab: (id: TabId) => void;
  /** `title === null` reverts the tab to its automatic title. */
  renameTab: (id: TabId, title: string | null) => void;
  reorderTab: (fromId: TabId, toId: TabId) => void;
  updateActiveTab: (patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
};
