import { create } from "zustand";

export type Lang = "en" | "zh";
export type Theme = "light" | "dark";

const LANG_KEY = "qy_react_lang";
const THEME_KEY = "qy_react_theme";
const SIDEBAR_WIDTH_KEY = "qy_react_sw";
const MAX_ROWS_KEY = "qy_react_maxrows";
const EDITOR_HEIGHT_KEY = "qy_react_edh";
const COLLAPSED_KEY = "qy_react_collapsed";

// The vanilla `/` GUI's equivalents — every simple UI-preference key it wrote
// is read here as a one-time fallback the first time its React-owned key has
// never been written, so an existing user's prefs survive the switch to
// `/app` (#53, localStorage consolidation). Legacy keys are left in place
// (never deleted) since the vanilla GUI at `/` still reads/writes them.
const LEGACY_LANG_KEY = "qy_lang";
const LEGACY_THEME_KEY = "qy_theme";
const LEGACY_SIDEBAR_WIDTH_KEY = "qy_sw";
const LEGACY_MAX_ROWS_KEY = "qy_maxrows";
const LEGACY_EDITOR_HEIGHT_KEY = "qy_edh";
const LEGACY_COLLAPSED_KEY = "qy_collapsed";

export const SIDEBAR_MIN = 200;
export const SIDEBAR_MAX = 480;
export const MAX_ROWS_OPTIONS = [100, 500, 2000, 5000];

// Reads `key`, falling back to `legacyKey` only the first time `key` has
// never been written — and, when that fallback fires, immediately writes the
// legacy value into `key` so the migration actually converges. Without this
// write-back the fallback re-runs on every load, which means a later edit to
// the legacy `/` GUI's key (e.g. toggling theme there) would keep leaking
// into `/app` instead of `/app` owning its own state (#53 review r1-1).
function readString(key: string, legacyKey: string): string | null {
  const v = localStorage.getItem(key);
  if (v !== null) return v;
  const legacy = localStorage.getItem(legacyKey);
  if (legacy !== null) {
    try {
      localStorage.setItem(key, legacy);
    } catch {
      // storage full/unavailable — the migrated value just won't persist
    }
  }
  return legacy;
}

function readLang(): Lang {
  return readString(LANG_KEY, LEGACY_LANG_KEY) === "zh" ? "zh" : "en";
}

function readTheme(): Theme {
  return readString(THEME_KEY, LEGACY_THEME_KEY) === "dark" ? "dark" : "light";
}

function readSidebarWidth(): number {
  const raw = Number(readString(SIDEBAR_WIDTH_KEY, LEGACY_SIDEBAR_WIDTH_KEY));
  return Number.isFinite(raw) && raw > 0 ? Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, raw)) : 280;
}

function readMaxRows(): number {
  const raw = Number(readString(MAX_ROWS_KEY, LEGACY_MAX_ROWS_KEY));
  return MAX_ROWS_OPTIONS.includes(raw) ? raw : 500;
}

function readEditorHeight(): number {
  const raw = Number(readString(EDITOR_HEIGHT_KEY, LEGACY_EDITOR_HEIGHT_KEY));
  return raw > 0 ? raw : 154;
}

// The legacy `/` GUI keys an ungrouped connection's collapse state by its
// *localized* "other"/"其他" label (`${ws}::other` / `${ws}::其他`) instead
// of an empty group name; React's own groupKey() always uses `${ws}::`
// regardless of language. Normalize that one mismatch on migration so a
// group collapsed under the old GUI still resolves under the new key format
// (#53 review r1-2).
const LEGACY_UNGROUPED_SUFFIXES = ["::other", "::其他"];

function normalizeLegacyGroupKey(key: string): string {
  for (const suffix of LEGACY_UNGROUPED_SUFFIXES) {
    if (key.endsWith(suffix)) return key.slice(0, key.length - suffix.length) + "::";
  }
  return key;
}

function readCollapsedGroups(): Set<string> {
  const own = localStorage.getItem(COLLAPSED_KEY);
  if (own !== null) {
    try {
      const parsed = JSON.parse(own);
      return new Set(Array.isArray(parsed) ? parsed : []);
    } catch {
      return new Set();
    }
  }
  try {
    const legacy = localStorage.getItem(LEGACY_COLLAPSED_KEY);
    if (legacy === null) return new Set();
    const parsed = JSON.parse(legacy);
    const migrated: string[] = Array.isArray(parsed) ? parsed.map(normalizeLegacyGroupKey) : [];
    try {
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(migrated));
    } catch {
      // storage full/unavailable — the migrated value just won't persist
    }
    return new Set(migrated);
  } catch {
    return new Set();
  }
}

function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

type UiState = {
  lang: Lang;
  theme: Theme;
  sidebarWidth: number;
  maxRows: number;
  editorHeight: number;
  collapsedGroups: Set<string>;
  setLang: (lang: Lang) => void;
  toggleLang: () => void;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  setSidebarWidth: (n: number) => void;
  setMaxRows: (n: number) => void;
  setEditorHeight: (n: number) => void;
  toggleCollapsedGroup: (key: string) => void;
};

/**
 * Owns every simple UI-preference key that used to be read/written ad hoc
 * from whichever component happened to render it (#53): language, theme,
 * sidebar width, max-rows cap, editor height, and collapsed sidebar groups.
 * Tabs + their results live in `tabsStore` instead — that state is keyed by
 * tab id and needs its own connection-aware restore/migration logic.
 */
export const useUiStore = create<UiState>((set, get) => {
  const theme = readTheme();
  applyTheme(theme);
  return {
    lang: readLang(),
    theme,
    sidebarWidth: readSidebarWidth(),
    maxRows: readMaxRows(),
    editorHeight: readEditorHeight(),
    collapsedGroups: readCollapsedGroups(),
    setLang: (lang) => {
      localStorage.setItem(LANG_KEY, lang);
      set({ lang });
    },
    toggleLang: () => get().setLang(get().lang === "en" ? "zh" : "en"),
    setTheme: (theme) => {
      localStorage.setItem(THEME_KEY, theme);
      applyTheme(theme);
      set({ theme });
    },
    toggleTheme: () => get().setTheme(get().theme === "light" ? "dark" : "light"),
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
      const rounded = Math.round(n);
      localStorage.setItem(EDITOR_HEIGHT_KEY, String(rounded));
      set({ editorHeight: rounded });
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
