import { create } from "zustand";

export type Theme = "light" | "dark";

// Same localStorage keys (and value formats) as the legacy GUI, so an existing
// user's preferences carry over unchanged — no migration layer needed.
const THEME_KEY = "qy_theme";
const SIDEBAR_WIDTH_KEY = "qy_sw";
const MAX_ROWS_KEY = "qy_maxrows";
const EDITOR_HEIGHT_KEY = "qy_edh";
const COLLAPSED_KEY = "qy_collapsed";

export const SIDEBAR_MIN = 150;
export const SIDEBAR_MAX = 480;
export const EDITOR_MIN = 70;
export const MAX_ROWS_OPTIONS = [100, 500, 2000, 5000];

function readTheme(): Theme {
  return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
}

function readNumber(key: string, fallback: number): number {
  const raw = Number(localStorage.getItem(key));
  return Number.isFinite(raw) && raw > 0 ? raw : fallback;
}

function readMaxRows(): number {
  const raw = Number(localStorage.getItem(MAX_ROWS_KEY));
  return MAX_ROWS_OPTIONS.includes(raw) ? raw : 500;
}

function readCollapsedGroups(): Set<string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "[]");
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch {
    return new Set();
  }
}

function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

type UiState = {
  theme: Theme;
  sidebarWidth: number;
  maxRows: number;
  editorHeight: number;
  collapsedGroups: Set<string>;
  /** Set when the backend restarted with a different version than the one
   * this page was loaded against — drives the "reload to upgrade" banner. */
  upgradedTo: string | null;
  toggleTheme: () => void;
  setSidebarWidth: (n: number) => void;
  setMaxRows: (n: number) => void;
  setEditorHeight: (n: number) => void;
  toggleCollapsedGroup: (key: string) => void;
  setUpgradedTo: (v: string | null) => void;
};

/** Simple UI preferences (theme, panel sizes, max-rows cap, collapsed sidebar
 * groups), each persisted under its legacy key. Language is NOT here — it is
 * fixed per page load (see i18n.ts), exactly like the legacy GUI. */
export const useUiStore = create<UiState>((set, get) => {
  const theme = readTheme();
  applyTheme(theme);
  return {
    theme,
    sidebarWidth: readNumber(SIDEBAR_WIDTH_KEY, 244),
    maxRows: readMaxRows(),
    editorHeight: readNumber(EDITOR_HEIGHT_KEY, 154),
    collapsedGroups: readCollapsedGroups(),
    upgradedTo: null,
    setUpgradedTo: (v) => set({ upgradedTo: v }),
    toggleTheme: () => {
      const next: Theme = get().theme === "dark" ? "light" : "dark";
      localStorage.setItem(THEME_KEY, next);
      applyTheme(next);
      set({ theme: next });
    },
    setSidebarWidth: (n) => {
      const clamped = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, n));
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(clamped));
      set({ sidebarWidth: clamped });
    },
    setMaxRows: (n) => {
      localStorage.setItem(MAX_ROWS_KEY, String(n));
      set({ maxRows: n });
    },
    setEditorHeight: (n) => {
      const clamped = Math.round(
        Math.min(window.innerHeight * 0.7, Math.max(EDITOR_MIN, n)),
      );
      localStorage.setItem(EDITOR_HEIGHT_KEY, String(clamped));
      set({ editorHeight: clamped });
    },
    toggleCollapsedGroup: (key) => {
      const next = new Set(get().collapsedGroups);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]));
      set({ collapsedGroups: next });
    },
  };
});
