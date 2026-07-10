import { useCallback, useRef, useState } from "react";

export type HistEntry = { sql: string; db: string | null; env: string | null; ts: number };

const STORAGE_KEY = "qy_react_hist";
const MAX_ENTRIES = 100;

function readHistory(): HistEntry[] {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
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

/**
 * Owns the SQL history list plus the "never silently lose a hand-written
 * draft" invariant: any site that's about to overwrite the editor (table
 * click, saved query, history recall, …) must call `keepDraft` first so the
 * draft is recoverable from history instead of vanishing.
 *
 * Cmd/Ctrl+Up/Down walks history without touching it (`navigateHistory`);
 * only `pushHist` (called on run, or via `keepDraft`) mutates the list.
 */
export function useSqlHistory() {
  const [history, setHistory] = useState<HistEntry[]>(() => readHistory());
  const hiRef = useRef(-1);
  const draftRef = useRef("");

  const pushHist = useCallback((sql: string, db: string | null, env: string | null) => {
    const trimmed = sql.trim();
    if (!trimmed) return;
    setHistory((prev) => {
      if (prev[0]?.sql === trimmed) return prev;
      const next = [{ sql: trimmed, db, env, ts: Date.now() }, ...prev].slice(0, MAX_ENTRIES);
      persist(next);
      return next;
    });
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
    (dir: "up" | "down", currentValue: string): string | null => {
      if (dir === "up") {
        if (hiRef.current >= history.length - 1) return null;
        if (hiRef.current === -1) draftRef.current = currentValue;
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
