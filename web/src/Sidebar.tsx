import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchHealth,
  fetchQueries,
  type ColumnsResponse,
  type ConnEnv,
  type ConnGroup,
  type ConnItem,
  type HealthResponse,
  type RedisKeyMeta,
  type SavedQuery,
} from "./api";

export type SidebarTarget = { db: string; env: string | null; label: string; engine: string };

type SelectOpts = { viaPill?: boolean };

type RTreeNode = { dirs: Record<string, RTreeNode>; leaves: RedisKeyMeta[] };

function groupKey(g: ConnGroup): string {
  return `${g.ws ?? ""}::${g.group ?? ""}`;
}

export function defaultEnvFor(item: ConnItem): string | null {
  return item.envs.find((e) => e.env === "dev")?.env ?? item.envs[0]?.env ?? null;
}

function buildRedisTree(keys: RedisKeyMeta[]): RTreeNode {
  const root: RTreeNode = { dirs: {}, leaves: [] };
  for (const k of keys) {
    const parts = k.key.split(":");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      if (!node.dirs[seg]) node.dirs[seg] = { dirs: {}, leaves: [] };
      node = node.dirs[seg];
    }
    node.leaves.push({ ...k, key: k.key });
  }
  return root;
}

function countRedisNode(n: RTreeNode): number {
  let c = n.leaves.length;
  for (const seg in n.dirs) c += countRedisNode(n.dirs[seg]);
  return c;
}

function fmtTtl(sec: number): string {
  if (sec < 0) return "";
  if (sec >= 86400) return `${Math.floor(sec / 86400)}d`;
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h`;
  if (sec >= 60) return `${Math.floor(sec / 60)}m`;
  return `${sec}s`;
}

function leafLabel(key: string): string {
  const parts = key.split(":");
  return parts[parts.length - 1] || key;
}

function HealthDot({ state }: { state: HealthResponse | undefined }) {
  const cls = state === undefined || state.ok === null ? "chk" : state.ok ? "ok" : "down";
  const title = state && state.ok === false ? state.error || "unreachable" : "";
  return <span className={`health-dot ${cls}`} title={title} />;
}

function RedisTree({
  node,
  path,
  collapsed,
  onToggle,
  onInspect,
}: {
  node: RTreeNode;
  path: string;
  collapsed: Set<string>;
  onToggle: (path: string) => void;
  onInspect: (key: string) => void;
}) {
  const dirNames = Object.keys(node.dirs).sort();
  return (
    <>
      {dirNames.map((name) => {
        const childPath = path ? `${path}:${name}` : name;
        const child = node.dirs[name];
        const isClosed = collapsed.has(childPath);
        return (
          <div key={childPath} className="rnode-wrap">
            <button
              type="button"
              className="tname knode"
              data-testid="redis-dir"
              onClick={() => onToggle(childPath)}
            >
              <span className="tw">{isClosed ? "▸" : "▾"}</span>
              {name}
              <span className="rbadge">{countRedisNode(child)}</span>
            </button>
            {!isClosed && (
              <div className="kchild">
                <RedisTree
                  node={child}
                  path={childPath}
                  collapsed={collapsed}
                  onToggle={onToggle}
                  onInspect={onInspect}
                />
              </div>
            )}
          </div>
        );
      })}
      {node.leaves.map((lf) => (
        <button
          key={lf.key}
          type="button"
          className="tname"
          data-testid="redis-key"
          data-key={lf.key}
          onClick={() => onInspect(lf.key)}
        >
          {leafLabel(lf.key)}
          <span className="rbadge">{lf.type}</span>
          {lf.ttl > 0 && <span className="rbadge ttl">{fmtTtl(lf.ttl)}</span>}
        </button>
      ))}
    </>
  );
}

function SavedQueryModal({
  query,
  onClose,
  onSubmit,
}: {
  query: SavedQuery;
  onClose: () => void;
  onSubmit: (params: Record<string, string>) => void;
}) {
  const inputsRef = useRef<Record<string, HTMLInputElement | null>>({});

  const submit = (): void => {
    const params: Record<string, string> = {};
    for (const p of query.params) {
      const el = inputsRef.current[p.name];
      if (el && el.value !== "") params[p.name] = el.value;
    }
    onSubmit(params);
  };

  return (
    <div
      id="saved-query-modal-backdrop"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Enter") submit();
      }}
    >
      <div id="saved-query-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>{query.name} · fill params</strong>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
        {query.desc && <p className="schema-status">{query.desc}</p>}
        <div className="modal-body">
          {query.params.map((p, i) => (
            <div key={p.name} className="param-field">
              <label>
                {p.name} <span className="param-type">{p.type || "text"}</span>
                {p.required
                  ? <span className="param-req">required</span>
                  : p.default != null
                    ? <span className="param-default">default {String(p.default)}</span>
                    : null}
              </label>
              <input
                data-testid={`saved-query-param-${p.name}`}
                autoFocus={i === 0}
                defaultValue={p.default != null ? String(p.default) : ""}
                ref={(el) => {
                  inputsRef.current[p.name] = el;
                }}
              />
            </div>
          ))}
          <div className="param-actions">
            <button type="button" id="saved-query-run-btn" onClick={submit}>
              Run
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export type SidebarProps = {
  groups: ConnGroup[];
  current: SidebarTarget | null;
  onSelect: (db: string, env: string | null, opts?: SelectOpts) => void;
  panelOpen: boolean;
  onTogglePanel: () => void;
  tablesEngine: string;
  tables: string[] | null;
  redisKeys: RedisKeyMeta[] | null;
  tablesError: string | null;
  tablesCapped: boolean;
  tableFilter: string;
  onTableFilterChange: (v: string) => void;
  selectedTable: string | null;
  onTableClick: (table: string) => void;
  onRefreshTables: () => void;
  onInspectKey: (key: string) => void;
  onRunSaved: (name: string, params: Record<string, string>) => void;
  columns: ColumnsResponse | null;
  columnsError: string | null;
  sidebarWidth: number;
  onSidebarResizeStart: (evt: React.MouseEvent) => void;
};

const COLLAPSE_KEY = "qy_react_collapsed";

function readCollapsed(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    return new Set();
  }
}

export default function Sidebar(props: SidebarProps) {
  const {
    groups,
    current,
    onSelect,
    panelOpen,
    onTogglePanel,
    tablesEngine,
    tables,
    redisKeys,
    tablesError,
    tablesCapped,
    tableFilter,
    onTableFilterChange,
    selectedTable,
    onTableClick,
    onRefreshTables,
    onInspectKey,
    onRunSaved,
    columns,
    columnsError,
    sidebarWidth,
    onSidebarResizeStart,
  } = props;

  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed());
  const [health, setHealth] = useState<Record<string, HealthResponse>>({});
  const [checking, setChecking] = useState(false);
  const [redisFold, setRedisFold] = useState<Set<string>>(new Set());
  const [savedQueries, setSavedQueries] = useState<SavedQuery[] | null>(null);
  const [paramModal, setParamModal] = useState<SavedQuery | null>(null);

  useEffect(() => {
    fetchQueries()
      .then(setSavedQueries)
      .catch(() => setSavedQueries([]));
  }, []);

  // instant cache paint + best-effort background probe for every connection.
  useEffect(() => {
    let cancelled = false;
    for (const g of groups) {
      for (const item of g.items) {
        const env = defaultEnvFor(item);
        fetchHealth(item.db, env, { cachedOnly: true })
          .then((res) => {
            if (cancelled || res.ok === null) return;
            setHealth((prev) => ({ ...prev, [item.db]: res }));
          })
          .catch(() => {});
      }
    }
    return () => {
      cancelled = true;
    };
  }, [groups]);

  const toggleGroup = (key: string): void => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...next]));
      return next;
    });
  };

  const checkHealth = async (): Promise<void> => {
    setChecking(true);
    const items: ConnItem[] = [];
    for (const g of groups) for (const it of g.items) items.push(it);
    const worker = async (start: number): Promise<void> => {
      for (let i = start; i < items.length; i += 3) {
        const it = items[i];
        const env = defaultEnvFor(it);
        try {
          const res = await fetchHealth(it.db, env, { fresh: true });
          setHealth((prev) => ({ ...prev, [it.db]: res }));
        } catch {
          setHealth((prev) => ({ ...prev, [it.db]: { ok: false, error: "probe failed" } }));
        }
      }
    };
    await Promise.all([worker(0), worker(1), worker(2)]);
    setChecking(false);
  };

  const toggleRedisFold = (path: string): void => {
    setRedisFold((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const handleRowClick = (db: string, env: string | null): void => {
    if (current && current.db === db && current.env === env) {
      onTogglePanel();
      return;
    }
    onSelect(db, env);
  };

  const handlePillClick = (db: string, env: ConnEnv): void => {
    onSelect(db, env.env, { viaPill: true });
  };

  const filteredTables = useMemo(() => {
    if (!tables) return [];
    const q = tableFilter.trim().toLowerCase();
    return q ? tables.filter((t) => t.toLowerCase().includes(q)) : tables;
  }, [tables, tableFilter]);

  const filteredRedisKeys = useMemo(() => {
    if (!redisKeys) return [];
    const q = tableFilter.trim().toLowerCase();
    return q ? redisKeys.filter((k) => k.key.toLowerCase().includes(q)) : redisKeys;
  }, [redisKeys, tableFilter]);

  const redisTree = useMemo(() => buildRedisTree(filteredRedisKeys), [filteredRedisKeys]);

  const runSavedClick = (q: SavedQuery): void => {
    if (q.params.length === 0) {
      onRunSaved(q.name, {});
      return;
    }
    setParamModal(q);
  };

  return (
    <aside className="conn-side" style={{ width: sidebarWidth }}>
      <div className="conn-tree-head">
        <button
          id="react-health-btn"
          type="button"
          className={checking ? "spin" : ""}
          onClick={() => void checkHealth()}
        >
          {checking ? "Checking…" : "Check health"}
        </button>
      </div>
      <div className="conn-tree" data-testid="conn-tree">
        {groups.map((g) => {
          const key = groupKey(g);
          const isCollapsed = collapsed.has(key);
          return (
            <div className="conn-group" key={key}>
              <button
                type="button"
                className="conn-group-head"
                data-testid="conn-group-toggle"
                data-group={key}
                onClick={() => toggleGroup(key)}
              >
                <span className="tw">{isCollapsed ? "▸" : "▾"}</span>
                <span className="conn-group-name">{g.group ?? "(ungrouped)"}</span>
                {g.ws && (
                  <span className="conn-group-ws" title={g.ws}>
                    {g.ws}
                  </span>
                )}
              </button>
              {!isCollapsed && (
                <div className="conn-group-body">
                  {g.items.map((item) => {
                    const env = defaultEnvFor(item);
                    const active = current?.db === item.db;
                    const h = health[item.db];
                    const multi = item.envs.length > 1;
                    return (
                      <div key={item.db} className="conn-item">
                        <button
                          type="button"
                          className={`conn-row${active ? " on" : ""}${h?.ok === false ? " down" : ""}`}
                          data-testid="conn-row"
                          data-db={item.db}
                          title={h?.ok === false ? h.error || "unreachable" : ""}
                          onClick={() => handleRowClick(item.db, env)}
                        >
                          <HealthDot state={h} />
                          <span className="conn-db-name">{item.db}</span>
                          <span className="conn-engine">{item.engine}</span>
                        </button>
                        {multi && (
                          <div className="pills" data-testid="env-pills">
                            {item.envs.map((e) => (
                              <button
                                key={e.env ?? ""}
                                type="button"
                                className={`env-pill${current?.db === item.db && current?.env === e.env ? " on" : ""}${(e.env || "").toLowerCase() === "prod" ? " prod" : ""}`}
                                data-env={e.env ?? ""}
                                onClick={() => handlePillClick(item.db, e)}
                              >
                                {e.env || "default"}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <input
        className="table-filter"
        type="search"
        placeholder={tablesEngine === "redis" ? "filter keys" : "filter tables"}
        value={tableFilter}
        onChange={(e) => onTableFilterChange(e.target.value)}
      />

      {panelOpen && current && (
        <div className="schema-tables" data-testid="schema-tables">
          <div className="panel-toolbar">
            <button
              type="button"
              className="table-refresh"
              data-testid="table-refresh-btn"
              title="refresh"
              onClick={onRefreshTables}
            >
              ↻
            </button>
          </div>
          {tablesError && <p className="schema-status schema-error">{tablesError}</p>}
          {!tablesError && tablesEngine === "redis" && tables === null && redisKeys === null && (
            <p className="schema-status">loading keys…</p>
          )}
          {!tablesError && tablesEngine !== "redis" && tables === null && (
            <p className="schema-status">loading tables…</p>
          )}
          {!tablesError && tablesEngine !== "redis" && tables !== null && filteredTables.length === 0 && (
            <p className="schema-status">no tables</p>
          )}
          {tablesCapped && tablesEngine !== "redis" && (
            <p className="schema-status">showing only the first 5000 tables</p>
          )}
          {!tablesError && tablesEngine !== "redis" && filteredTables.length > 0 && (
            <ul>
              {filteredTables.map((tbl) => (
                <li key={tbl}>
                  <button
                    type="button"
                    className={tbl === selectedTable ? "active" : ""}
                    onClick={() => onTableClick(tbl)}
                  >
                    {tbl}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {!tablesError && tablesEngine === "redis" && redisKeys !== null && redisKeys.length === 0 && (
            <p className="schema-status">no keys</p>
          )}
          {tablesCapped && tablesEngine === "redis" && redisKeys && (
            <p className="schema-status">showing only the first {redisKeys.length} keys</p>
          )}
          {tablesEngine === "redis" && redisKeys && redisKeys.length > 0 && (
            <div className="ktree" data-testid="redis-tree">
              <RedisTree
                node={redisTree}
                path=""
                collapsed={redisFold}
                onToggle={toggleRedisFold}
                onInspect={onInspectKey}
              />
            </div>
          )}
        </div>
      )}

      {panelOpen && current && tablesEngine !== "redis" && (
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
      )}

      {savedQueries !== null && savedQueries.length > 0 && (
        <div className="saved-queries" data-testid="saved-queries">
          <div className="saved-queries-head">Saved queries</div>
          <ul>
            {savedQueries.map((q) => (
              <li key={q.name}>
                <button
                  type="button"
                  data-testid="saved-query-item"
                  title={q.desc ?? undefined}
                  onClick={() => runSavedClick(q)}
                >
                  <span className="qname">{q.name}</span>
                  {q.params.length > 0 && <span className="rbadge">{q.params.length}</span>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {paramModal && (
        <SavedQueryModal
          query={paramModal}
          onClose={() => setParamModal(null)}
          onSubmit={(params) => {
            setParamModal(null);
            onRunSaved(paramModal.name, params);
          }}
        />
      )}

      <div
        id="sidebar-resizer"
        className="sidebar-resizer"
        onMouseDown={onSidebarResizeStart}
      />
    </aside>
  );
}
