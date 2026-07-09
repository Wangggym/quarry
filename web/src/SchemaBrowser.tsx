import { useEffect, useMemo, useState } from "react";
import { fetchColumns, fetchConnections, fetchTables } from "./api";
import type { ColumnsResponse, ConnectionsResponse } from "./api";

type Target = { db: string; env: string | null; label: string };

function flattenTargets(data: ConnectionsResponse): Target[] {
  const out: Target[] = [];
  for (const g of data.groups) {
    for (const item of g.items) {
      for (const e of item.envs) {
        const label = e.env ? `${item.db}@${e.env}` : item.db;
        out.push({ db: item.db, env: e.env, label });
      }
    }
  }
  return out;
}

/** Sidebar table-structure browser: pick a connection, list its tables, and
 * show column name + type for the selected table (issue #11). Reuses the
 * existing /api/connections, /api/tables, /api/columns contract — no backend
 * behavior change beyond api_columns() now also returning per-column types. */
export default function SchemaBrowser() {
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [tables, setTables] = useState<string[] | null>(null);
  const [tablesEngine, setTablesEngine] = useState<string>("");
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [columns, setColumns] = useState<ColumnsResponse | null>(null);
  const [columnsError, setColumnsError] = useState<string | null>(null);

  useEffect(() => {
    fetchConnections()
      .then((data) => {
        const flat = flattenTargets(data);
        setTargets(flat);
        if (flat.length > 0) setSelected(flat[0].label);
      })
      .catch(() => setTargets([]));
  }, []);

  const current = useMemo(
    () => targets?.find((t) => t.label === selected) ?? null,
    [targets, selected],
  );

  // Both effects below guard against stale responses with a per-run `cancelled`
  // closure: switching the connection (or table) again re-runs the effect,
  // React invokes the PREVIOUS run's cleanup first, and that flips its
  // `cancelled` flag so a still-in-flight older response can never repaint
  // state for a selection the user has already moved away from (latest-wins).
  useEffect(() => {
    if (!current) return;
    let cancelled = false;
    setTables(null);
    setTablesError(null);
    setSelectedTable(null);
    setColumns(null);
    fetchTables(current.db, current.env)
      .then((res) => {
        if (cancelled) return;
        setTablesEngine(res.engine);
        setTables("tables" in res ? res.tables : []);
      })
      .catch((e) => {
        if (cancelled) return;
        setTablesError(String(e.message ?? e));
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
        if (cancelled) return;
        setColumnsError(String(e.message ?? e));
      });
    return () => {
      cancelled = true;
    };
  }, [current, selectedTable]);

  if (targets === null) {
    return <p className="schema-status">loading connections…</p>;
  }
  if (targets.length === 0) {
    return <p className="schema-status">no connections configured</p>;
  }

  return (
    <div className="schema-browser">
      <div className="schema-toolbar">
        <label htmlFor="schema-conn-select">connection</label>
        <select
          id="schema-conn-select"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          {targets.map((t) => (
            <option key={t.label} value={t.label}>
              {t.label}
            </option>
          ))}
        </select>
      </div>
      <div className="schema-panes">
        <div className="schema-tables" data-testid="schema-tables">
          {tablesError && <p className="schema-status schema-error">{tablesError}</p>}
          {!tablesError && tablesEngine === "redis" && (
            <p className="schema-status">redis has no table schema</p>
          )}
          {!tablesError && tables === null && tablesEngine !== "redis" && (
            <p className="schema-status">loading tables…</p>
          )}
          {!tablesError && tables !== null && tables.length === 0 && tablesEngine !== "redis" && (
            <p className="schema-status">no tables</p>
          )}
          {tables && tables.length > 0 && (
            <ul>
              {tables.map((tbl) => (
                <li key={tbl}>
                  <button
                    type="button"
                    className={tbl === selectedTable ? "active" : ""}
                    onClick={() => setSelectedTable(tbl)}
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
          {selectedTable && !columnsError && columns === null && (
            <p className="schema-status">loading columns…</p>
          )}
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
                {columns.columns.map((c) => (
                  <tr key={c}>
                    <td>{c}</td>
                    <td className="schema-type">{columns.types[c] ?? "?"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
