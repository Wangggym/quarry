export type ConnEnv = {
  env: string | null;
  key: string;
  engine: string;
  region: string | null;
  ssh: boolean;
};

export type VersionInfo = { name: string; version: string };

export type ConnItem = {
  db: string;
  is_env_set: boolean;
  engine: string;
  envs: ConnEnv[];
};

export type ConnGroup = {
  group: string | null;
  ws: string | null;
  items: ConnItem[];
};

export type ConnectionsResponse = {
  groups: ConnGroup[];
  workspace: string;
  workspaces: string[];
};

export type TablesResponse =
  | { engine: "redis"; keys: unknown[]; capped: boolean }
  | { engine: string; tables: string[]; capped: boolean };

export type ColumnsResponse = {
  columns: string[];
  types: Record<string, string | null>;
};

export type QueryColumn = {
  name: string;
  type: string | null;
};

export type QueryResult = {
  columns: QueryColumn[];
  rows: Record<string, unknown>[];
  rowCount: number;
  truncated: boolean;
  elapsedMs: number;
  engine: string;
  sql: string;
};

export type QueryRequest = {
  db: string;
  env: string | null;
  sql: string;
  maxRows: number;
  offset?: number;
};

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(body.error || `${path} -> ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(payload.error || `${path} -> ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export function fetchVersion(): Promise<VersionInfo> {
  return getJSON("/api/version");
}

export function fetchConnections(): Promise<ConnectionsResponse> {
  return getJSON("/api/connections");
}

export function fetchTables(db: string, env: string | null): Promise<TablesResponse> {
  const qs = new URLSearchParams({ db, env: env ?? "" });
  return getJSON(`/api/tables?${qs}`);
}

export function fetchColumns(
  db: string,
  env: string | null,
  table: string,
): Promise<ColumnsResponse> {
  const qs = new URLSearchParams({ db, env: env ?? "", table });
  return getJSON(`/api/columns?${qs}`);
}

export function runQuery(req: QueryRequest): Promise<QueryResult> {
  return postJSON("/api/query", req);
}
