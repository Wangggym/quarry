import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchColumns,
  fetchConnections,
  fetchInspect,
  fetchTables,
  runQuery,
  runSaved,
  type ColumnsResponse,
  type ConnectionsResponse,
  type QueryColumn,
  type RedisKeyMeta,
} from "./api";
import Sidebar, { defaultEnvFor, type SidebarTarget } from "./Sidebar";
import SqlEditor from "./SqlEditor";
import TabBar from "./TabBar";
import { useConnMetaStore } from "./store/connStore";
import { useTabsStore } from "./store/tabsStore";
import type { Tab, TabId, TabResultSnapshot } from "./store/types";
import { useSqlHistory } from "./useSqlHistory";

type Target = SidebarTarget;

const SIDEBAR_WIDTH_KEY = "qy_react_sw";
const SIDEBAR_MIN = 200;
const SIDEBAR_MAX = 480;
const MAX_ROWS_KEY = "qy_react_maxrows";
type Row = Record<string, unknown>;
type SortState = { colIndex: number; dir: "asc" | "desc" } | null;
type SelectedCell = { rowIndex: number; colIndex: number } | null;
type ModalState =
  | { type: "json"; title: string; value: unknown }
  | { type: "row"; title: string; row: Row }
  | { type: "explain"; title: string; plan: string }
  | null;

const EMPTY_SNAPSHOT: TabResultSnapshot = { result: null, queryDb: null, queryEnv: null, querySql: null };

/** Snapshot of the connection a request was fired against — used to detect a
 * tab re-pointed to another connection while the request was in flight. */
type ReqCtx = { tabId: TabId; seq: number; db: string; env: string | null };

const MAX_ROWS_OPTIONS = [100, 500, 2000, 5000];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const NUMERIC_RE = /^[-+]?\d+(\.\d+)?$/;
const TIMESTAMP_RE = /^\d{4}-\d\d-\d\d(?:[ T]\d\d:\d\d(:\d\d(\.\d+)?)?)?/;

function flattenTargets(data: ConnectionsResponse): Target[] {
  const out: Target[] = [];
  for (const g of data.groups) {
    for (const item of g.items) {
      for (const env of item.envs) {
        const label = env.env ? `${item.db}@${env.env}` : item.db;
        out.push({ db: item.db, env: env.env, label, engine: item.engine });
      }
    }
  }
  return out;
}

function quoteIdent(name: string, engine: string): string {
  const bare = /^[a-z_][a-z0-9_]*$/;
  if (bare.test(name)) return name;
  if (engine === "mysql") return `\`${name.replaceAll("`", "``")}\``;
  return `"${name.replaceAll('"', '""')}"`;
}

function classifyCell(value: unknown, colType?: string | null): string {
  const t = (colType || "").toLowerCase();
  if (value === null) return "null";
  if (typeof value === "boolean" || t.includes("bool")) return "bool";
  if (typeof value === "number" || NUMERIC_RE.test(String(value))) return "num";
  if (typeof value === "object") return "json";
  const text = String(value);
  if (t.includes("uuid") || UUID_RE.test(text)) return "uuid";
  if (t.includes("time") || t.includes("date") || TIMESTAMP_RE.test(text)) return "ts";
  if (
    t.includes("json") ||
    ((text.startsWith("{") || text.startsWith("[")) && (() => {
      try {
        JSON.parse(text);
        return true;
      } catch {
        return false;
      }
    })())
  ) {
    return "json";
  }
  return "";
}

function parseMaybeJson(value: unknown): unknown {
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return null;
  const s = value.trim();
  if ((!s.startsWith("{") && !s.startsWith("[")) || s.length < 2) return null;
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const raw = typeof value === "string" ? value : JSON.stringify(value);
  return `"${raw.replaceAll('"', '""')}"`;
}

/** Relative-time label for a history entry's timestamp (History modal). */
function fmtAgo(ts: number): string {
  const s = (Date.now() - ts) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function triggerDownload(content: string, filename: string, mime: string): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function sortRows(rows: Row[], columns: QueryColumn[], state: SortState): Row[] {
  if (!state) return rows;
  const col = columns[state.colIndex];
  if (!col) return rows;
  const key = col.name;
  const type = col.type?.toLowerCase() ?? "";
  const sign = state.dir === "asc" ? 1 : -1;
  const sorted = [...rows].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === null || av === undefined) return bv === null || bv === undefined ? 0 : 1;
    if (bv === null || bv === undefined) return -1;
    const as = String(av);
    const bs = String(bv);
    const numeric =
      typeof av === "number" ||
      typeof bv === "number" ||
      type.includes("int") ||
      type.includes("float") ||
      type.includes("numeric") ||
      (NUMERIC_RE.test(as) && NUMERIC_RE.test(bs));
    if (numeric) {
      const an = Number(av);
      const bn = Number(bv);
      if (!Number.isNaN(an) && !Number.isNaN(bn) && an !== bn) return (an - bn) * sign;
    }
    return as.localeCompare(bs, undefined, { numeric: true }) * sign;
  });
  return sorted;
}

function JsonTree({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    return (
      <ul className="jt-list">
        {value.map((v, i) => (
          <li key={i}>
            <span className="jt-key">{i}</span>: <JsonTree value={v} />
          </li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    return (
      <ul className="jt-list">
        {Object.entries(value).map(([k, v]) => (
          <li key={k}>
            <span className="jt-key">{k}</span>: <JsonTree value={v} />
          </li>
        ))}
      </ul>
    );
  }
  return <span className="jt-leaf">{JSON.stringify(value)}</span>;
}

export default function ResultWorkbench() {
  const [connData, setConnData] = useState<ConnectionsResponse | null>(null);
  const [selected, setSelected] = useState("");
  const [panelOpen, setPanelOpen] = useState(true);
  const [tables, setTables] = useState<string[] | null>(null);
  const [redisKeys, setRedisKeys] = useState<RedisKeyMeta[] | null>(null);
  const [tablesEngine, setTablesEngine] = useState("");
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [tablesCapped, setTablesCapped] = useState(false);
  const [tableFilter, setTableFilter] = useState("");
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [columns, setColumns] = useState<ColumnsResponse | null>(null);
  const [columnsError, setColumnsError] = useState<string | null>(null);
  const tabs = useTabsStore((s) => s.tabs);
  const activeTabId = useTabsStore((s) => s.activeId);
  const switchTab = useTabsStore((s) => s.switchTab);
  const closeTab = useTabsStore((s) => s.closeTab);
  const updateActiveTab = useTabsStore((s) => s.updateActiveTab);
  const updateTab = useTabsStore((s) => s.updateTab);
  const results = useTabsStore((s) => s.results);
  const setTabResult = useTabsStore((s) => s.setTabResult);
  const activeTab = useMemo(
    () => tabs.find((t) => t.id === activeTabId) ?? tabs[0],
    [tabs, activeTabId],
  );
  const sql = activeTab.sql;
  const setSql = (v: string): void => updateActiveTab({ sql: v });
  const [maxRows, setMaxRowsState] = useState<number>(() => {
    const raw = Number(localStorage.getItem(MAX_ROWS_KEY));
    return MAX_ROWS_OPTIONS.includes(raw) ? raw : 500;
  });
  const setMaxRows = (n: number): void => {
    setMaxRowsState(n);
    localStorage.setItem(MAX_ROWS_KEY, String(n));
  };
  const [explainBusy, setExplainBusy] = useState(false);
  const [historySearch, setHistorySearch] = useState("");
  // Per-tab in-flight bookkeeping (#51, connection isolation): a request's
  // sequence number is snapshotted at start, so a later request on the same
  // tab always wins (latest-wins), and a response is only ever applied to —
  // or shown an error on — the tab that fired it.
  const reqSeqRef = useRef<Record<TabId, number>>({});
  const [pendingByTab, setPendingByTab] = useState<Record<TabId, boolean>>({});
  const [errorByTab, setErrorByTab] = useState<Record<TabId, string | null>>({});
  const loading = pendingByTab[activeTabId] ?? false;
  const gridError = errorByTab[activeTabId] ?? null;
  // The result actually painted is the active tab's own snapshot as-is — an
  // in-place connection switch (env pill, sidebar click) while this SAME tab
  // stays active must never touch the grid (mirrors the legacy editor: only
  // ARRIVING at a tab, via switch/close-landing/reload, re-validates it
  // against the connection that's current at that moment; see
  // `revalidateTabResult` below and `readInitialResults` in tabsStore.ts).
  const activeSnapshot = results[activeTabId] ?? EMPTY_SNAPSHOT;
  const { result, queryDb, queryEnv, querySql } = activeSnapshot;
  const [sortState, setSortState] = useState<SortState>(null);
  const [selectedCell, setSelectedCell] = useState<SelectedCell>(null);
  const [modal, setModal] = useState<ModalState>(null);
  const [columnWidths, setColumnWidths] = useState<Record<number, number>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    const raw = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
    return Number.isFinite(raw) && raw > 0
      ? Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, raw))
      : 280;
  });
  const gridWrapRef = useRef<HTMLDivElement | null>(null);
  const tablesReqIdRef = useRef(0);
  const { history, pushHist, keepDraft, navigateHistory } = useSqlHistory();

  useEffect(() => {
    fetchConnections()
      .then((data) => {
        setConnData(data);
        // Prefer restoring the persisted active tab's own connection; fall
        // back to the first connection only if that tab has none, or it no
        // longer resolves to a live target.
        const restoreState = useTabsStore.getState();
        const restoreTab = restoreState.tabs.find((t) => t.id === restoreState.activeId);
        if (restoreTab?.db) {
          const restored = flattenTargets(data).find(
            (t) => t.db === restoreTab.db && t.env === restoreTab.env,
          );
          if (restored) {
            setSelected(restored.label);
            return;
          }
        }
        const firstItem = data.groups[0]?.items[0];
        if (firstItem) {
          const env = defaultEnvFor(firstItem);
          setSelected(env ? `${firstItem.db}@${env}` : firstItem.db);
        }
      })
      .catch(() => setConnData({ groups: [], workspace: "", workspaces: [] }));
  }, []);

  // Mirrored (not moved) into connStore so Header.tsx can render the
  // workspace label / prod badge / conn-info button without this component
  // handing down its request-tracking internals.
  useEffect(() => {
    if (!connData) return;
    useConnMetaStore.getState().setConnMeta(connData.workspace, connData.workspaces, connData.groups);
  }, [connData]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2200);
    return () => window.clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    if (!historyOpen) setHistorySearch("");
  }, [historyOpen]);

  // Grid/status are always view-only projections of the active tab's own
  // result; sort/selection never carry over from whichever tab was active
  // before (mirrors the legacy editor's showTabResult() always resetting
  // sortState on a tab switch).
  useEffect(() => {
    setSortState(null);
    setSelectedCell(null);
  }, [activeTabId]);

  useEffect(() => {
    if (!modal && !historyOpen) return;
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key !== "Escape") return;
      if (modal) setModal(null);
      else setHistoryOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [modal, historyOpen]);

  const targets = useMemo(() => (connData ? flattenTargets(connData) : null), [connData]);

  const current = useMemo(
    () => targets?.find((t) => t.label === selected) ?? null,
    [targets, selected],
  );

  useEffect(() => {
    useConnMetaStore
      .getState()
      .setCurrent(current ? { db: current.db, env: current.env, engine: current.engine } : null);
  }, [current]);

  // Keep the active tab's db/env in sync with whichever connection is
  // currently selected (mirrors the legacy editor's saveUI()).
  useEffect(() => {
    if (!current) return;
    updateActiveTab({ db: current.db, env: current.env });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- updateActiveTab is a stable store action
  }, [current?.db, current?.env]);

  const syncSelectedToTab = useCallback(
    (tab: Tab | undefined): void => {
      const target = tab?.db ? targets?.find((t) => t.db === tab.db && t.env === tab.env) : null;
      setSelected(target ? target.label : "");
    },
    [targets],
  );

  // ARRIVING at a tab (switch, or landing on the adjacent one after closing
  // the active tab) re-validates its stored result against ITS OWN current
  // connection — mirrors the legacy editor's showTabResult(). A same-tab
  // in-place connection switch never runs this (see `activeSnapshot` above),
  // so a rebind only surfaces once the user actually leaves and returns.
  const revalidateTabResult = useCallback(
    (tab: Tab | undefined): void => {
      if (!tab) return;
      const snap = useTabsStore.getState().results[tab.id];
      if (!snap?.result) return;
      if (snap.queryDb !== (tab.db ?? null) || (snap.queryEnv ?? null) !== (tab.env ?? null)) {
        setTabResult(tab.id, EMPTY_SNAPSHOT);
      }
    },
    [setTabResult],
  );

  const handleTabSwitch = useCallback(
    (tab: Tab): void => {
      switchTab(tab.id);
      revalidateTabResult(tab);
      syncSelectedToTab(tab);
    },
    [switchTab, revalidateTabResult, syncSelectedToTab],
  );

  const handleTabClose = useCallback(
    (tab: Tab): void => {
      // Closing a tab must never silently lose hand-written SQL.
      const dying = tab.sql.trim();
      if (dying) pushHist(dying, tab.db, tab.env);
      closeTab(tab.id);
      const next = useTabsStore.getState();
      if (next.activeId !== activeTabId) {
        const newActive = next.tabs.find((t) => t.id === next.activeId);
        revalidateTabResult(newActive);
        syncSelectedToTab(newActive);
      }
    },
    [pushHist, closeTab, activeTabId, revalidateTabResult, syncSelectedToTab],
  );

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (!(e.metaKey || e.ctrlKey) || !e.shiftKey) return;
      if (e.key !== "w" && e.key !== "W") return;
      if (document.querySelector(".tab.renaming")) return;
      const state = useTabsStore.getState();
      if (state.tabs.length <= 1) return;
      e.preventDefault();
      const dying = state.tabs.find((t) => t.id === state.activeId);
      if (dying) handleTabClose(dying);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [handleTabClose]);

  const loadTables = (target: Target, fresh: boolean): void => {
    const id = ++tablesReqIdRef.current;
    setTablesError(null);
    fetchTables(target.db, target.env, { fresh })
      .then((res) => {
        if (tablesReqIdRef.current !== id) return; // stale response — a later request has already won
        setTablesEngine(res.engine);
        setTablesCapped(!!res.capped);
        if ("tables" in res) {
          setTables(res.tables);
          setRedisKeys(null);
        } else {
          setRedisKeys(res.keys);
          setTables(null);
        }
      })
      .catch((e) => {
        if (tablesReqIdRef.current === id) setTablesError(String(e.message ?? e));
      });
  };

  useEffect(() => {
    if (!current) return;
    setTables(null);
    setRedisKeys(null);
    setTablesCapped(false);
    setSelectedTable(null);
    setColumns(null);
    loadTables(current, false);
  }, [current]);

  const refreshTables = (): void => {
    if (current) loadTables(current, true);
  };

  useEffect(() => {
    if (!current || !selectedTable) return;
    let cancelled = false;
    setColumns(null);
    setColumnsError(null);
    fetchColumns(current.db, current.env, selectedTable)
      .then((res) => {
        if (!cancelled) setColumns(res);
      })
      .catch((e) => {
        if (!cancelled) setColumnsError(String(e.message ?? e));
      });
    return () => {
      cancelled = true;
    };
  }, [current, selectedTable]);

  // The table highlight is only meaningful while the editor still holds the
  // `limit 5` preview it generated; once the user edits it away, drop it.
  useEffect(() => {
    if (!selectedTable || !current) return;
    const expected = `select * from ${quoteIdent(selectedTable, current.engine)} limit 5`;
    if (sql.trim() !== expected) setSelectedTable(null);
  }, [sql, selectedTable, current]);

  const shownRows = useMemo(
    () => sortRows(result?.rows ?? [], result?.columns ?? [], sortState),
    [result, sortState],
  );
  // Only offered while the tab's CURRENT connection still matches the one
  // that produced the shown grid — an in-place connection switch leaves the
  // grid on screen (see `activeSnapshot` above) but must not let "Load more"
  // page in more rows from underneath a connection the user has since left.
  const canLoadMore = !!(
    result &&
    result.truncated &&
    querySql &&
    current &&
    current.db === queryDb &&
    (current.env ?? null) === (queryEnv ?? null) &&
    (result.engine === "postgres" || result.engine === "mysql")
  );
  const sortArrow = (i: number): string => {
    if (!sortState || sortState.colIndex !== i) return "";
    return sortState.dir === "asc" ? "↑" : "↓";
  };

  // startReq/isCurrentReq/endReq mirror the legacy editor's TABREQ+runSeq
  // guard: a request snapshots its issuing tab id and the connection it was
  // fired against, so a later request on that SAME tab always wins
  // (latest-wins), independent of response arrival order.
  const startReq = (tabId: TabId, target: { db: string; env: string | null }): ReqCtx => {
    const seq = (reqSeqRef.current[tabId] ?? 0) + 1;
    reqSeqRef.current[tabId] = seq;
    setPendingByTab((prev) => ({ ...prev, [tabId]: true }));
    setErrorByTab((prev) => ({ ...prev, [tabId]: null }));
    return { tabId, seq, db: target.db, env: target.env };
  };
  const isCurrentReq = (ctx: ReqCtx): boolean => reqSeqRef.current[ctx.tabId] === ctx.seq;
  const endReq = (ctx: ReqCtx): void => {
    if (!isCurrentReq(ctx)) return;
    setPendingByTab((prev) => ({ ...prev, [ctx.tabId]: false }));
  };
  const reqFailed = (ctx: ReqCtx, e: unknown): void => {
    // An error is tagged and persisted per-tab exactly like a successful
    // result (see `applyTabResult`): it's stored under its issuing tab even
    // while that tab is in the background, and only becomes visible once the
    // user returns to it (`gridError` looks it up by `activeTabId`). It's
    // dropped, like a result, if that tab was re-pointed to a different
    // connection while the request was in flight — never mislabeled onto the
    // new connection.
    if (!isCurrentReq(ctx)) return;
    const tab = useTabsStore.getState().tabs.find((t) => t.id === ctx.tabId);
    if (!tab) return; // tab closed while in flight
    if (tab.db !== ctx.db || (tab.env ?? null) !== (ctx.env ?? null)) return;
    setErrorByTab((prev) => ({ ...prev, [ctx.tabId]: String((e as Error)?.message ?? e) }));
  };
  /** Applies a fresh result to its origin tab — but only if that request is
   * still the latest for that tab, the tab still exists, and the tab's
   * CURRENT connection still equals the one the request was fired against
   * (a re-point mid-flight must drop the response, never mislabel it). */
  const applyTabResult = (
    ctx: ReqCtx,
    build: () => TabResultSnapshot,
    opts?: { resetSort?: boolean },
  ): void => {
    if (!isCurrentReq(ctx)) return;
    const state = useTabsStore.getState();
    const tab = state.tabs.find((t) => t.id === ctx.tabId);
    if (!tab) return; // tab closed while in flight
    if (tab.db !== ctx.db || (tab.env ?? null) !== (ctx.env ?? null)) return; // re-pointed mid-flight -> drop
    setTabResult(ctx.tabId, build());
    if (opts?.resetSort !== false && state.activeId === ctx.tabId) {
      setSortState(null);
      setSelectedCell(null);
    }
  };

  const run = async (overrideTarget?: Target): Promise<void> => {
    const target = overrideTarget ?? current;
    if (!target || !sql.trim()) return;
    pushHist(sql, target.db, target.env);
    const tabId = activeTabId;
    const ctx = startReq(tabId, target);
    try {
      const data = await runQuery({ db: target.db, env: target.env, sql, maxRows, offset: 0 });
      applyTabResult(ctx, () => ({ result: data, queryDb: target.db, queryEnv: target.env, querySql: sql }));
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  /** Light SQL formatter: collapse whitespace, uppercase major keywords,
   * newline before major clauses. React port of the legacy `#fmtBtn` handler
   * — not a real SQL parser, just the same regex-based touch-up. */
  const formatSql = (): void => {
    let s = sql;
    s = s.replace(/\s+/g, " ").replace(/\s*,\s*/g, ", ").trim();
    s = s.replace(
      /\b(select|from|where|order by|group by|having|limit|offset|left join|right join|inner join|join|on|and|or|union|values|insert into|update|set|delete from)\b/gi,
      (m) => m.toUpperCase(),
    );
    s = s.replace(
      /\b(FROM|WHERE|ORDER BY|GROUP BY|HAVING|LIMIT|OFFSET|LEFT JOIN|RIGHT JOIN|INNER JOIN|JOIN|UNION)\b/g,
      "\n$1",
    );
    setSql(s);
  };

  /** EXPLAIN the current SQL. Guards mirror the legacy `#expBtn` handler:
   * no-connection/redis show a toast instead of firing; a plan with more
   * than one column (e.g. mysql's tabular EXPLAIN) falls through to the
   * normal grid via `applyTabResult` instead of the modal; the modal itself
   * is suppressed if the issuing tab is no longer the active one, or has
   * been re-pointed to another connection, by the time the response lands. */
  const runExplain = async (): Promise<void> => {
    if (!current) {
      setToast("Pick a connection first");
      return;
    }
    if (tablesEngine === "redis") {
      setToast("No query plan for redis");
      return;
    }
    const trimmed = sql.trim();
    if (!trimmed) return;
    setExplainBusy(true);
    const tabId = activeTabId;
    const ctx = startReq(tabId, current);
    try {
      const explainSql = `EXPLAIN ${trimmed.replace(/^\s*explain\s+/i, "")}`;
      const data = await runQuery({ db: current.db, env: current.env, sql: explainSql, maxRows, offset: 0 });
      if (data.columns.length > 1) {
        applyTabResult(ctx, () => ({
          result: data,
          queryDb: current.db,
          queryEnv: current.env,
          querySql: explainSql,
        }));
        return;
      }
      if (!isCurrentReq(ctx)) return; // superseded by a newer request on this tab
      const state = useTabsStore.getState();
      const tab = state.tabs.find((t) => t.id === ctx.tabId);
      if (!tab || state.activeId !== ctx.tabId) return; // tab switched away mid-flight
      if (tab.db !== ctx.db || (tab.env ?? null) !== (ctx.env ?? null)) return; // re-pointed mid-flight
      const col = data.columns[0]?.name;
      const plan = col ? data.rows.map((r) => String(r[col] ?? "")).join("\n") : "(empty plan)";
      setModal({
        type: "explain",
        title: `EXPLAIN · ${current.db}${current.env ? `@${current.env}` : ""}`,
        plan,
      });
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
      setExplainBusy(false);
    }
  };

  const handleSelect = (db: string, env: string | null, opts?: { viaPill?: boolean }): void => {
    const target =
      targets?.find((t) => t.db === db && t.env === env) ?? targets?.find((t) => t.db === db);
    if (!target) return;
    setSelected(target.label);
    const isProd = (env || "").toLowerCase() === "prod";
    // Switching to prod must never auto-fire the current query; switching
    // between non-prod envs via a pill re-runs it against the new target.
    if (opts?.viaPill && !isProd) void run(target);
  };

  const handleTableClick = (tbl: string): void => {
    if (!current) return;
    const next = `select * from ${quoteIdent(tbl, current.engine)} limit 5`;
    keepDraft(sql, next, current.db, current.env);
    setSelectedTable(tbl);
    setSql(next);
  };

  const handleInspectKey = async (key: string): Promise<void> => {
    if (!current) return;
    const placeholder = `# ${key}`;
    keepDraft(sql, placeholder, current.db, current.env);
    setSql(placeholder);
    setSelectedTable(null);
    const tabId = activeTabId;
    const ctx = startReq(tabId, current);
    try {
      const data = await fetchInspect(current.db, current.env, key);
      applyTabResult(ctx, () => ({
        result: data,
        queryDb: current.db,
        queryEnv: current.env,
        querySql: null,
      }));
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  // Saved queries are tagged by the connection the response actually
  // resolved to, NOT the tab's connection at fire time — `@db` can be a
  // logical env-set, so the response's own db/env always wins and the tab is
  // retagged to match, independent of whether a matching sidebar entry
  // exists. Unlike run()/handleInspectKey(), a mid-flight re-point of the
  // ISSUING tab must not drop the response — it's the saved query itself
  // that decides the tab's new connection.
  const handleRunSaved = async (name: string, params: Record<string, string>): Promise<void> => {
    const tabId = activeTabId;
    const ctx = startReq(tabId, { db: current?.db ?? "", env: current?.env ?? null });
    try {
      const data = await runSaved(name, current?.env ?? null, params, maxRows);
      if (!isCurrentReq(ctx)) return;
      const state = useTabsStore.getState();
      const tab = state.tabs.find((t) => t.id === tabId);
      if (!tab) return; // tab closed while in flight
      const db = data.db ?? tab.db;
      const env = data.env ?? null;
      updateTab(tabId, { db, env });
      setTabResult(tabId, { result: data, queryDb: db, queryEnv: env, querySql: null });
      if (state.activeId === tabId) {
        setSortState(null);
        setSelectedCell(null);
        setSelectedTable(null);
        const target = targets?.find((t) => t.db === db && t.env === env);
        setSelected(target ? target.label : "");
        keepDraft(sql, data.sql, current?.db ?? null, current?.env ?? null);
        setSql(data.sql);
      }
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  const startSidebarResize = (evt: React.MouseEvent): void => {
    evt.preventDefault();
    const startX = evt.clientX;
    const startWidth = sidebarWidth;
    const onMove = (moveEvt: MouseEvent): void => {
      const next = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, startWidth + (moveEvt.clientX - startX)));
      setSidebarWidth(next);
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(next));
    };
    const onUp = (): void => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  // Fires against `queryDb`/`queryEnv` — the connection that produced the
  // page being extended — never against `current`. `canLoadMore` already
  // keeps the button hidden once the tab's current connection drifts from
  // that, but firing the request itself against the producing connection
  // means that if the tab gets re-pointed mid-flight anyway, `applyTabResult`
  // drops the response instead of appending rows from a connection the tab
  // no longer points at.
  const onLoadMore = async (): Promise<void> => {
    if (!current || !querySql || !queryDb || !result || !canLoadMore) return;
    const tabId = activeTabId;
    const ctx = startReq(tabId, { db: queryDb, env: queryEnv });
    const prevRows = result.rows;
    const prevElapsed = result.elapsedMs;
    try {
      const data = await runQuery({
        db: queryDb,
        env: queryEnv,
        sql: querySql,
        maxRows,
        offset: prevRows.length,
      });
      applyTabResult(
        ctx,
        () => {
          const merged = [...prevRows, ...data.rows];
          return {
            result: { ...data, rows: merged, rowCount: merged.length, elapsedMs: prevElapsed + data.elapsedMs },
            queryDb,
            queryEnv,
            querySql,
          };
        },
        { resetSort: false },
      );
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  const onSort = (colIndex: number): void => {
    setSortState((prev) => {
      if (!prev || prev.colIndex !== colIndex) return { colIndex, dir: "asc" };
      if (prev.dir === "asc") return { colIndex, dir: "desc" };
      return null;
    });
  };

  const onCellOpen = async (row: Row, col: QueryColumn): Promise<void> => {
    const value = row[col.name];
    const parsed = parseMaybeJson(value);
    if (parsed !== null) {
      setModal({ type: "json", title: col.name, value: parsed });
      return;
    }
    const text = value === null || value === undefined ? "null" : String(value);
    if (text.length <= 120) {
      try {
        await navigator.clipboard.writeText(text);
        setToast("Copied cell value");
      } catch {
        setModal({ type: "json", title: col.name, value: text });
      }
      return;
    }
    setModal({ type: "json", title: col.name, value: text });
  };

  const onGridKeyDown = (evt: React.KeyboardEvent<HTMLDivElement>): void => {
    if (!result || shownRows.length === 0 || !selectedCell) return;
    const maxRow = shownRows.length - 1;
    const maxCol = result.columns.length - 1;
    if (evt.key === "ArrowDown") {
      evt.preventDefault();
      setSelectedCell({ rowIndex: Math.min(maxRow, selectedCell.rowIndex + 1), colIndex: selectedCell.colIndex });
    } else if (evt.key === "ArrowUp") {
      evt.preventDefault();
      setSelectedCell({ rowIndex: Math.max(0, selectedCell.rowIndex - 1), colIndex: selectedCell.colIndex });
    } else if (evt.key === "ArrowRight") {
      evt.preventDefault();
      setSelectedCell({ rowIndex: selectedCell.rowIndex, colIndex: Math.min(maxCol, selectedCell.colIndex + 1) });
    } else if (evt.key === "ArrowLeft") {
      evt.preventDefault();
      setSelectedCell({ rowIndex: selectedCell.rowIndex, colIndex: Math.max(0, selectedCell.colIndex - 1) });
    } else if (evt.key === "Enter") {
      evt.preventDefault();
      const row = shownRows[selectedCell.rowIndex];
      const col = result.columns[selectedCell.colIndex];
      if (row && col) void onCellOpen(row, col);
    }
  };

  const startResize = (evt: React.MouseEvent, colIndex: number): void => {
    evt.preventDefault();
    const startX = evt.clientX;
    const currentWidth = columnWidths[colIndex] ?? 180;
    const onMove = (moveEvt: MouseEvent): void => {
      const next = Math.max(80, currentWidth + (moveEvt.clientX - startX));
      setColumnWidths((prev) => ({ ...prev, [colIndex]: next }));
    };
    const onUp = (): void => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const exportCsv = (): void => {
    if (!result) return;
    const header = result.columns.map((c) => csvCell(c.name)).join(",");
    const body = shownRows
      .map((r) => result.columns.map((c) => csvCell(r[c.name])).join(","))
      .join("\n");
    const full = `\ufeff${header}\n${body}`;
    triggerDownload(full, `quarry-${queryDb ?? "result"}.csv`, "text/csv");
  };

  const exportJson = (): void => {
    if (!result) return;
    triggerDownload(JSON.stringify(shownRows, null, 2), `quarry-${queryDb ?? "result"}.json`, "application/json");
  };

  const recallHistory = (entrySql: string): void => {
    keepDraft(sql, entrySql, current?.db ?? null, current?.env ?? null);
    setSql(entrySql);
    setHistoryOpen(false);
  };

  const filteredHistory = useMemo(() => {
    const q = historySearch.trim().toLowerCase();
    if (!q) return history;
    return history.filter(
      (h) => h.sql.toLowerCase().includes(q) || (h.db ?? "").toLowerCase().includes(q),
    );
  }, [history, historySearch]);

  if (targets === null) return <p className="schema-status">loading connections…</p>;
  if (targets.length === 0) return <p className="schema-status">no connections configured</p>;

  return (
    <section className="workbench">
      <div className="workbench-bar">
        <span className="run-target">{current ? `${current.db}${current.env ? `@${current.env}` : ""}` : "-"}</span>
      </div>

      <div className="workbench-body" style={{ gridTemplateColumns: `${sidebarWidth}px 1fr` }}>
        <Sidebar
          groups={connData?.groups ?? []}
          current={current}
          onSelect={handleSelect}
          panelOpen={panelOpen}
          onTogglePanel={() => setPanelOpen((v) => !v)}
          tablesEngine={tablesEngine}
          tables={tables}
          redisKeys={redisKeys}
          tablesError={tablesError}
          tablesCapped={tablesCapped}
          tableFilter={tableFilter}
          onTableFilterChange={setTableFilter}
          selectedTable={selectedTable}
          onTableClick={handleTableClick}
          onRefreshTables={refreshTables}
          onInspectKey={(key) => void handleInspectKey(key)}
          onRunSaved={(name, params) => void handleRunSaved(name, params)}
          columns={columns}
          columnsError={columnsError}
          sidebarWidth={sidebarWidth}
          onSidebarResizeStart={startSidebarResize}
        />

        <div className="result-main">
          <TabBar onSwitch={handleTabSwitch} onClose={handleTabClose} />
          <div className="query-toolbar">
            <SqlEditor
              value={sql}
              onChange={setSql}
              onRun={() => void run()}
              db={current?.db ?? null}
              env={current?.env ?? null}
              isRedis={tablesEngine === "redis"}
              tables={tables ?? []}
              resultColumns={result?.columns.map((c) => c.name) ?? []}
              navigateHistory={navigateHistory}
            />
            <div className="query-actions">
              <label htmlFor="react-max-rows">Max rows</label>
              <select
                id="react-max-rows"
                value={String(maxRows)}
                onChange={(e) => setMaxRows(Number(e.target.value))}
              >
                {MAX_ROWS_OPTIONS.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              <button id="react-run-btn" type="button" disabled={!current || loading || !sql.trim()} onClick={() => void run()}>
                {loading ? "Running…" : "Run"}
              </button>
              <button id="react-format-btn" type="button" disabled={!sql.trim()} onClick={formatSql}>
                Format
              </button>
              <button
                id="react-explain-btn"
                type="button"
                disabled={!current || explainBusy || !sql.trim()}
                onClick={() => void runExplain()}
              >
                {explainBusy ? "EXPLAIN…" : "EXPLAIN"}
              </button>
              <button id="react-csv-btn" type="button" disabled={!result} onClick={exportCsv}>
                CSV
              </button>
              <button id="react-json-btn" type="button" disabled={!result} onClick={exportJson}>
                JSON
              </button>
              <button id="react-history-btn" type="button" onClick={() => setHistoryOpen(true)}>
                History{history.length > 0 ? ` (${history.length})` : ""}
              </button>
            </div>
          </div>

          <div
            className="grid-wrap"
            id="react-grid-wrap"
            ref={gridWrapRef}
            tabIndex={0}
            onKeyDown={onGridKeyDown}
            onClick={() => gridWrapRef.current?.focus()}
          >
            {loading && !result && <p className="grid-state">running query…</p>}
            {gridError && <p className="grid-state grid-error">{gridError}</p>}
            {!loading && !gridError && result && shownRows.length === 0 && <p className="grid-state">0 rows</p>}
            {!loading && !gridError && !result && <p className="grid-state">run a query to view results</p>}
            {result && shownRows.length > 0 && (
              <table id="react-grid">
                <colgroup>
                  <col style={{ width: 56 }} />
                  {result.columns.map((_, i) => (
                    <col key={i} style={{ width: columnWidths[i] ?? 180 }} />
                  ))}
                </colgroup>
                <thead>
                  <tr>
                    <th>#</th>
                    {result.columns.map((c, i) => (
                      <th key={c.name} className="resizable">
                        <button type="button" className="th-btn" onClick={() => onSort(i)}>
                          <span>{c.name}</span>
                          <span className="arrow">{sortArrow(i)}</span>
                        </button>
                        <span className="col-type">{c.type ?? "?"}</span>
                        <span className="rz" onMouseDown={(e) => startResize(e, i)} />
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shownRows.map((r, rowIndex) => (
                    <tr key={rowIndex}>
                      <td className="rownum" onClick={() => setModal({ type: "row", title: `Row ${rowIndex + 1}`, row: r })}>
                        {rowIndex + 1}
                      </td>
                      {result.columns.map((c, colIndex) => {
                        const value = r[c.name];
                        const isSel =
                          selectedCell?.rowIndex === rowIndex && selectedCell?.colIndex === colIndex;
                        return (
                          <td
                            key={`${rowIndex}-${c.name}`}
                            className={`${classifyCell(value, c.type)} ${isSel ? "sel" : ""}`}
                            data-v={value === null || value === undefined ? "null" : String(value)}
                            onClick={() => setSelectedCell({ rowIndex, colIndex })}
                            onDoubleClick={() => void onCellOpen(r, c)}
                          >
                            {value === null ? "null" : typeof value === "object" ? JSON.stringify(value) : String(value)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {result && (
            <div id="react-status" className="status-bar">
              <span>{result.rowCount} rows</span>
              <span>{result.elapsedMs} ms</span>
              <span>{queryDb}{queryEnv ? `@${queryEnv}` : ""}</span>
              {result.truncated && <span className="truncated">truncated to cap</span>}
            </div>
          )}
          {canLoadMore && (
            <button id="react-load-more" type="button" disabled={loading} onClick={() => void onLoadMore()}>
              {loading ? "Loading…" : "Load more"}
            </button>
          )}
        </div>
      </div>

      {toast && <div id="react-toast">{toast}</div>}
      {modal && (
        <div id="react-modal-backdrop" onClick={() => setModal(null)}>
          <div id="react-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <strong>{modal.title}</strong>
              <button type="button" onClick={() => setModal(null)}>
                Close
              </button>
            </div>
            {modal.type === "json" && (
              <div className="modal-body">
                {typeof modal.value === "object" ? <JsonTree value={modal.value} /> : <pre>{String(modal.value)}</pre>}
              </div>
            )}
            {modal.type === "row" && (
              <div className="modal-body">
                <pre>{JSON.stringify(modal.row, null, 2)}</pre>
              </div>
            )}
            {modal.type === "explain" && (
              <div className="modal-body">
                <pre>{modal.plan}</pre>
              </div>
            )}
          </div>
        </div>
      )}
      {historyOpen && (
        <div id="react-history-backdrop" onClick={() => setHistoryOpen(false)}>
          <div id="react-history-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <strong>History</strong>
              <button type="button" onClick={() => setHistoryOpen(false)}>
                Close
              </button>
            </div>
            <div className="modal-body">
              <input
                id="react-history-search"
                data-testid="history-search"
                placeholder="Search history…"
                value={historySearch}
                onChange={(e) => setHistorySearch(e.target.value)}
              />
              {history.length === 0 && (
                <p className="hist-empty" data-testid="history-empty">
                  No history yet
                </p>
              )}
              {history.length > 0 && filteredHistory.length === 0 && (
                <p className="hist-empty" data-testid="history-empty">
                  No matches
                </p>
              )}
              {filteredHistory.map((h, i) => (
                <button
                  key={i}
                  type="button"
                  className="hist-item"
                  onClick={() => recallHistory(h.sql)}
                >
                  <pre>{h.sql}</pre>
                  <span className="hist-meta">
                    {h.db ? `${h.db}${h.env ? `@${h.env}` : ""} · ` : ""}
                    {fmtAgo(h.ts)}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
