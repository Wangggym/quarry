export type QueryLinkPayload = {
  db: string;
  env: string | null;
  sql: string;
};

function normalizeEnv(env: string | null): string | null {
  if (env === null) return null;
  return env.trim() === "" ? null : env;
}

export function encodeQueryLink(baseHref: string, payload: QueryLinkPayload): string {
  const url = new URL(baseHref);
  const params = new URLSearchParams(url.search);
  params.set("db", payload.db);
  params.set("sql", payload.sql);
  if (payload.env) params.set("env", payload.env);
  else params.delete("env");
  url.search = params.toString();
  return url.toString();
}

export function decodeQueryLink(search: string): QueryLinkPayload | null {
  const params = new URLSearchParams(search);
  const db = params.get("db");
  const sql = params.get("sql");
  if (!db || sql === null) return null;
  return { db, env: normalizeEnv(params.get("env")), sql };
}
