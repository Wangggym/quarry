import { useCallback, useRef, useState } from "react";

export type HistEntry = { sql: string; db: string | null; env: string | null; ts: number };

const STORAGE_KEY = "qy_react_hist";
// The vanilla `/` GUI's history key — read as a one-time fallback the first
// time `qy_react_hist` has never been written (#53, localStorage
// consolidation). Left in place, never deleted: the `/` GUI still uses it.
const LEGACY_STORAGE_KEY = "qy_hist";
const MAX_ENTRIES = 100;

function readHistory(): HistEntry[] {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? localStorage.getItem(LEGACY_STORAGE_KEY) ?? "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function persist(entries: HistEntry[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // storage full/unavailable — history just won't survive a reload
  }
}

function unshift(prev: HistEntry[], sql: string, db: string | null, env: string | null): HistEntry[] {
  if (prev[0]?.sql === sql) return prev;
  const next = [{ sql, db, env, ts: Date.now() }, ...prev].slice(0, MAX_ENTRIES);
  persist(next);
  return next;
}

/**
 * Owns the SQL history list plus the "never silently lose a hand-written
 * draft" invariant: any site that's about to overwrite the editor (table
 * click, saved query, history recall, …) must call `keepDraft` first so the
 * draft is recoverable from history instead of vanishing.
 *
 * Cmd/Ctrl+Up/Down walks history without touching it (`navigateHistory`);
 * only `pushHist` (called on run, or via `keepDraft`) mutates the list.
 *
 * Subtlety: while mid-navigation (`hiRef` pointing at a recalled entry, not
 * -1), the user's ORIGINAL hand-written draft lives only in `draftRef` — it
 * is neither in `history` nor the `current` value any overwrite site sees.
 * `pushHist` is the single choke point that resets `hiRef` back to -1
 * (whether reached directly, e.g. running the recalled query, or via
 * `keepDraft`, e.g. clicking a table while mid-navigation), so it also
 * rescues that orphaned draft into history first — otherwise it would
 * become silently unreachable the moment `hiRef` resets.
 */
export function useSqlHistory() {
  const [history, setHistory] = useState<HistEntry[]>(() => readHistory());
  const hiRef = useRef(-1);
  const draftRef = useRef("");
  const draftMetaRef = useRef<{ db: string | null; env: string | null }>({ db: null, env: null });

  const pushHist = useCallback((sql: string, db: string | null, env: string | null) => {
    if (hiRef.current !== -1) {
      const orphan = draftRef.current.trim();
      if (orphan && orphan !== sql.trim()) {
        setHistory((prev) => unshift(prev, orphan, draftMetaRef.current.db, draftMetaRef.current.env));
      }
    }
    const trimmed = sql.trim();
    if (trimmed) setHistory((prev) => unshift(prev, trimmed, db, env));
    hiRef.current = -1;
  }, []);

  const keepDraft = useCallback(
    (current: string, next: string, db: string | null, env: string | null) => {
      const s = current.trim();
      if (s && s !== next.trim()) pushHist(s, db, env);
    },
    [pushHist],
  );

  // Walks history in-place without recording it; the in-progress draft is
  // stashed on the way down and restored once navigation returns past the
  // most recent entry (mirrors the legacy editor's Cmd/Ctrl+Up/Down).
  const navigateHistory = useCallback(
    (
      dir: "up" | "down",
      currentValue: string,
      db: string | null = null,
      env: string | null = null,
    ): string | null => {
      if (dir === "up") {
        if (hiRef.current >= history.length - 1) return null;
        if (hiRef.current === -1) {
          draftRef.current = currentValue;
          draftMetaRef.current = { db, env };
        }
        hiRef.current += 1;
        return history[hiRef.current]?.sql ?? null;
      }
      if (hiRef.current <= -1) return null;
      if (hiRef.current === 0) {
        hiRef.current = -1;
        return draftRef.current;
      }
      hiRef.current -= 1;
      return history[hiRef.current]?.sql ?? null;
    },
    [history],
  );

  return { history, pushHist, keepDraft, navigateHistory };
}
