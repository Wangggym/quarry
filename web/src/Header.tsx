import { useState } from "react";
import ConnInfoModal from "./ConnInfoModal";
import WorkspaceModal from "./WorkspaceModal";
import { useConnMetaStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";
import { t } from "./i18n";

/** Header controls beyond the brand/version already rendered by App.tsx:
 * workspace label (+ multi-workspace tooltip), read-only/prod badges,
 * language + theme toggles, and the connection-info / workspace-manager
 * modal launchers. React port of the legacy GUI's `<header>` chrome. */
export default function Header() {
  const workspace = useConnMetaStore((s) => s.workspace);
  const workspaces = useConnMetaStore((s) => s.workspaces);
  const current = useConnMetaStore((s) => s.current);
  const lang = useUiStore((s) => s.lang);
  const theme = useUiStore((s) => s.theme);
  const toggleLang = useUiStore((s) => s.toggleLang);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const [connInfoOpen, setConnInfoOpen] = useState(false);
  const [wsOpen, setWsOpen] = useState(false);

  const isProd = (current?.env ?? "").toLowerCase() === "prod";
  const multiWs = workspaces.length > 1;

  return (
    <div className="header-controls" data-testid="header-controls">
      <span className="ws-label" data-testid="ws-label" title={workspaces.join("\n")}>
        {multiWs ? `${workspaces.length} workspaces` : workspace}
      </span>
      <button
        type="button"
        id="react-ws-btn"
        className="iconbtn"
        title={t(lang, "manageWorkspaces")}
        onClick={() => setWsOpen(true)}
      >
        ⚙
      </button>
      <span className="header-sp" />
      {isProd && (
        <span className="badge prod" id="react-prod-badge">
          {t(lang, "prod")}
        </span>
      )}
      <span className="badge ro" id="react-ro-badge">
        {t(lang, "readOnly")}
      </span>
      {current && (
        <button
          type="button"
          id="react-conninfo-btn"
          className="iconbtn"
          title={t(lang, "connInfo")}
          onClick={() => setConnInfoOpen(true)}
        >
          ⓘ
        </button>
      )}
      <button
        type="button"
        id="react-lang-btn"
        className="iconbtn"
        title={t(lang, "switchLang")}
        onClick={toggleLang}
      >
        {lang === "en" ? "中" : "EN"}
      </button>
      <button
        type="button"
        id="react-theme-btn"
        className="iconbtn"
        title={t(lang, "toggleTheme")}
        onClick={toggleTheme}
      >
        {theme === "light" ? "☾" : "☀"}
      </button>

      {connInfoOpen && current && (
        <ConnInfoModal db={current.db} env={current.env} onClose={() => setConnInfoOpen(false)} />
      )}
      {wsOpen && <WorkspaceModal onClose={() => setWsOpen(false)} />}
    </div>
  );
}
