import { create } from "zustand";

export type Lang = "en" | "zh";
export type Theme = "light" | "dark";

const LANG_KEY = "qy_react_lang";
const THEME_KEY = "qy_react_theme";

type UiState = {
  lang: Lang;
  theme: Theme;
  setLang: (lang: Lang) => void;
  toggleLang: () => void;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
};

function readLang(): Lang {
  return localStorage.getItem(LANG_KEY) === "zh" ? "zh" : "en";
}

function readTheme(): Theme {
  return localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
}

function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export const useUiStore = create<UiState>((set, get) => {
  const theme = readTheme();
  applyTheme(theme);
  return {
    lang: readLang(),
    theme,
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
  };
});
