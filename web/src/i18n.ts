import type { Lang } from "./store/uiStore";

/**
 * Scoped to header + toolbar strings only (issue #52) — not a full-app i18n
 * system like the legacy `/` GUI's exhaustive dictionary + page reload. This
 * toggle re-renders in place; the legacy one reloads the page.
 */
const DICT: Record<Lang, Record<string, string>> = {
  en: {
    run: "Run",
    running: "Running…",
    format: "Format",
    explain: "EXPLAIN",
    csv: "CSV",
    json: "JSON",
    history: "History",
    maxRows: "Max rows",
    checkHealth: "Check all connections",
    toggleTheme: "Toggle theme",
    switchLang: "切换到中文",
    connInfo: "Connection details",
    manageWorkspaces: "Manage workspaces",
    readOnly: "read-only · auto LIMIT",
    prod: "prod",
    noHistory: "No history yet",
    noMatch: "No matches",
    searchHistory: "Search history…",
    reveal: "reveal",
    hide: "hide",
    copy: "copy",
    createLocalEnv: "Create local env",
    syncSchemaFrom: "Sync schema from {env}",
    close: "Close",
    add: "Add",
  },
  zh: {
    run: "运行",
    running: "运行中…",
    format: "格式化",
    explain: "执行计划",
    csv: "CSV",
    json: "JSON",
    history: "历史",
    maxRows: "最大行数",
    checkHealth: "检查所有连接",
    toggleTheme: "切换主题",
    switchLang: "Switch to English",
    connInfo: "连接详情",
    manageWorkspaces: "管理工作区",
    readOnly: "只读 · 自动 LIMIT",
    prod: "生产",
    noHistory: "暂无历史记录",
    noMatch: "无匹配结果",
    searchHistory: "搜索历史…",
    reveal: "显示",
    hide: "隐藏",
    copy: "复制",
    createLocalEnv: "创建本地环境",
    syncSchemaFrom: "从 {env} 同步结构",
    close: "关闭",
    add: "添加",
  },
};

export function t(lang: Lang, key: string, vars?: Record<string, string>): string {
  let s = DICT[lang]?.[key] ?? DICT.en[key] ?? key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  }
  return s;
}
