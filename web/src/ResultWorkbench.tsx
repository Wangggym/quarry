import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchColumns,
  fetchConnections,
  fetchTables,
  runQuery,
  type ColumnsResponse,
  type ConnectionsResponse,
  type QueryColumn,
  type QueryResult,
} from "./api";

type Target = { db: string; env: string | null; label: string; engine: string };
type Row = Record<string, unknown>;
type SortState = { colIndex: number; dir: "asc" | "desc" } | null;
type SelectedCell = { rowIndex: number; colIndex: number } | null;
type ModalState =
  | { type: "json"; title: string; value: unknown }
  | { type: "row"; title: string; row: Row }
  | null;

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

function quoteIdent(name: string): string {
  const bare = /^[a-z_][a-z0-9_]*$/;
  if (bare.test(name)) return name;
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
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [selected, setSelected] = useState("");
  const [tables, setTables] = useState<string[] | null>(null);
  const [tablesEngine, setTablesEngine] = useState("");
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [tablesCapped, setTablesCapped] = useState(false);
  const [tableFilter, setTableFilter] = useState("");
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [columns, setColumns] = useState<ColumnsResponse | null>(null);
  const [columnsError, setColumnsError] = useState<string | null>(null);
  const [sql, setSql] = useState("");
  const [maxRows, setMaxRows] = useState(500);
  const [loading, setLoading] = useState(false);
  const [gridError, setGridError] = useState<string | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [baseRows, setBaseRows] = useState<Row[]>([]);
  const [sortState, setSortState] = useState<SortState>(null);
  const [selectedCell, setSelectedCell] = useState<SelectedCell>(null);
  const [modal, setModal] = useState<ModalState>(null);
  const [columnWidths, setColumnWidths] = useState<Record<number, number>>({});
  const [queryDb, setQueryDb] = useState<string | null>(null);
  const [queryEnv, setQueryEnv] = useState<string | null>(null);
  const [querySql, setQuerySql] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const gridWrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    fetchConnections()
      .then((data) => {
        const flat = flattenTargets(data);
        setTargets(flat);
        if (flat.length > 0) setSelected(flat[0].label);
      })
      .catch(() => setTargets([]));
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2200);
    return () => window.clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    if (!modal) return;
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === "Escape") setModal(null);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [modal]);

  const current = useMemo(
    () => targets?.find((t) => t.label === selected) ?? null,
    [targets, selected],
  );

  useEffect(() => {
    if (!current) return;
    let cancelled = false;
    setTables(null);
    setTablesError(null);
    setTablesCapped(false);
    setSelectedTable(null);
    setColumns(null);
    fetchTables(current.db, current.env)
      .then((res) => {
        if (cancelled) return;
        setTablesEngine(res.engine);
        setTablesCapped(!!res.capped);
        setTables("tables" in res ? res.tables : []);
      })
      .catch((e) => {
        if (!cancelled) setTablesError(String(e.message ?? e));
      });
    return () => {
      cancelled = true;
    };
  }, [current]);

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

  const filteredTables = useMemo(() => {
    if (!tables) return [];
    const key = tableFilter.trim().toLowerCase();
    if (!key) return tables;
    return tables.filter((t) => t.toLowerCase().includes(key));
  }, [tables, tableFilter]);

  const shownRows = useMemo(() => sortRows(baseRows, result?.columns ?? [], sortState), [baseRows, result, sortState]);
  const canLoadMore = !!(
    result &&
    result.truncated &&
    querySql &&
    (result.engine === "postgres" || result.engine === "mysql")
  );
  const sortArrow = (i: number): string => {
    if (!sortState || sortState.colIndex !== i) return "";
    return sortState.dir === "asc" ? "↑" : "↓";
  };

  const run = async (offset = 0): Promise<void> => {
    if (!current || !sql.trim()) return;
    setLoading(true);
    setGridError(null);
    try {
      const data = await runQuery({
        db: current.db,
        env: current.env,
        sql,
        maxRows,
        offset,
      });
      if (offset > 0) {
        const merged = [...baseRows, ...data.rows];
        setBaseRows(merged);
        setResult({
          ...data,
          rows: merged,
          rowCount: merged.length,
          elapsedMs: (result?.elapsedMs ?? 0) + data.elapsedMs,
        });
      } else {
        setResult(data);
        setBaseRows(data.rows);
        setSortState(null);
        setSelectedCell(null);
      }
      setQueryDb(current.db);
      setQueryEnv(current.env);
      setQuerySql(sql);
    } catch (e) {
      setGridError(String((e as Error)?.message ?? e));
      if (offset === 0) {
        setResult(null);
        setBaseRows([]);
        setSortState(null);
      }
    } finally {
      setLoading(false);
    }
  };

  const onLoadMore = async (): Promise<void> => {
    if (!querySql || !queryDb) return;
    setLoading(true);
    setGridError(null);
    try {
      const data = await runQuery({
        db: queryDb,
        env: queryEnv,
        sql: querySql,
        maxRows,
        offset: baseRows.length,
      });
      const merged = [...baseRows, ...data.rows];
      setBaseRows(merged);
      setResult((prev) =>
        prev
          ? {
              ...data,
              rows: merged,
              rowCount: merged.length,
              elapsedMs: prev.elapsedMs + data.elapsedMs,
            }
          : data,
      );
    } catch (e) {
      setGridError(String((e as Error)?.message ?? e));
    } finally {
      setLoading(false);
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

  if (targets === null) return <p className="schema-status">loading connections…</p>;
  if (targets.length === 0) return <p className="schema-status">no connections configured</p>;

  return (
    <section className="workbench">
      <div className="workbench-bar">
        <label htmlFor="schema-conn-select">Connection</label>
        <select id="schema-conn-select" value={selected} onChange={(e) => setSelected(e.target.value)}>
          {targets.map((t) => (
            <option key={t.label} value={t.label}>
              {t.label}
            </option>
          ))}
        </select>
        <span className="run-target">{current ? `${current.db}${current.env ? `@${current.env}` : ""}` : "-"}</span>
      </div>

      <div className="workbench-body">
        <aside className="schema-side">
          <input
            className="table-filter"
            type="search"
            placeholder="filter tables"
            value={tableFilter}
            onChange={(e) => setTableFilter(e.target.value)}
          />
          <div className="schema-tables" data-testid="schema-tables">
            {tablesError && <p className="schema-status schema-error">{tablesError}</p>}
            {!tablesError && tablesEngine === "redis" && <p className="schema-status">redis has no table schema</p>}
            {!tablesError && tables === null && <p className="schema-status">loading tables…</p>}
            {!tablesError && tables !== null && filteredTables.length === 0 && <p className="schema-status">no tables</p>}
            {tablesCapped && <p className="schema-status">showing only the first 5000 tables</p>}
            {filteredTables.length > 0 && (
              <ul>
                {filteredTables.map((tbl) => (
                  <li key={tbl}>
                    <button
                      type="button"
                      className={tbl === selectedTable ? "active" : ""}
                      onClick={() => {
                        setSelectedTable(tbl);
                        setSql(`select * from ${quoteIdent(tbl)} limit 5`);
                      }}
                    >
                      {tbl}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="schema-columns" data-testid="schema-columns">
            {!selectedTable && <p className="schema-status">select a table to see its columns</p>}
            {columnsError && <p className="schema-status schema-error">{columnsError}</p>}
            {selectedTable && !columnsError && columns === null && <p className="schema-status">loading columns…</p>}
            {columns && columns.columns.length === 0 && <p className="schema-status">no columns</p>}
            {columns && columns.columns.length > 0 && (
              <table className="schema-columns-table">
                <thead>
                  <tr>
                    <th>column</th>
                    <th>type</th>
                  </tr>
                </thead>
                <tbody>
                  {columns.columns.map((name) => (
                    <tr key={name}>
                      <td>{name}</td>
                      <td className="schema-type">{columns.types[name] ?? "?"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </aside>

        <div className="result-main">
          <div className="query-toolbar">
            <textarea
              id="react-sql-input"
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              placeholder="Write SQL and run"
              rows={4}
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
              <button id="react-csv-btn" type="button" disabled={!result} onClick={exportCsv}>
                CSV
              </button>
              <button id="react-json-btn" type="button" disabled={!result} onClick={exportJson}>
                JSON
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
          </div>
        </div>
      )}
    </section>
  );
}
