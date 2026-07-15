import { useState } from "react";
import { fetchHealth } from "./api";
import { t, LANG, toggleLang } from "./i18n";
import { useConnStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";
import UpdatePanel from "./UpdatePanel";
import WorkspaceModal from "./WorkspaceModal";

/** The header bar: brand, workspace label, workspace manager, prod/read-only
 * badges, health-check-all, language and theme toggles — the legacy GUI's
 * `<header>` chrome, same DOM and icons. */
export default function Header() {
  const workspace = useConnStore((s) => s.workspace);
  const workspaces = useConnStore((s) => s.workspaces);
  const groups = useConnStore((s) => s.groups);
  const current = useConnStore((s) => s.current);
  const checking = useConnStore((s) => s.checking);
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const updateInfo = useUiStore((s) => s.updateInfo);
  const [wsOpen, setWsOpen] = useState(false);
  const [updOpen, setUpdOpen] = useState(false);

  const isProd = (current?.env ?? "").toLowerCase() === "prod";
  const multiWs = workspaces.length > 1;

  /** Probe every connection (concurrency-capped at 3 — parallel SSH tunnels
   * skew each other's results) and paint the sidebar dots as results land. */
  const checkHealth = async (): Promise<void> => {
    const { setChecking, setHealth } = useConnStore.getState();
    setChecking(true);
    const items = groups.flatMap((g) => g.items);
    let i = 0;
    const worker = async (): Promise<void> => {
      while (i < items.length) {
        const it = items[i++];
        const env = it.envs.find((e) => e.env === "dev")?.env ?? it.envs[0]?.env ?? "";
        try {
          const d = await fetchHealth(it.db, env, { fresh: true });
          setHealth(it.db, !!d.ok, d.error);
        } catch {
          setHealth(it.db, false);
        }
      }
    };
    await Promise.all(Array.from({ length: 3 }, worker));
    setChecking(false);
  };

  return (
    <header>
      <div className="logo">Q</div>
      <span className="brand">Quarry</span>
      <span className="ws" id="ws" title={workspaces.join("\n")}>
        {multiWs ? (
          <>
            <i className="ti ti-stack-2" /> {workspaces.length} workspaces
          </>
        ) : (
          <>
            <i className="ti ti-folder" /> {workspace}
          </>
        )}
      </span>
      <button
        className="iconbtn"
        id="wsBtn"
        title={t("ws_manage")}
        aria-label={t("ws_manage")}
        onClick={() => setWsOpen(true)}
      >
        <i className="ti ti-settings" />
      </button>
      <span className="sp" />
      <span className="badge prod" id="prodBadge" style={{ display: isProd ? undefined : "none" }}>
        <i className="ti ti-alert-triangle" /> prod
      </span>
      <span className="badge ro" id="roBadge">
        <i className="ti ti-lock" /> {t("ro_badge")}
      </span>
      {updateInfo?.available && (
        <button className="badge update" id="updateBadge" onClick={() => setUpdOpen(true)}>
          <span className="update-dot" /> {t("update_available")}
        </button>
      )}
      <button
        className={`iconbtn${checking ? " spin" : ""}`}
        id="healthBtn"
        title={t("check_health")}
        aria-label={t("check_health")}
        onClick={() => void checkHealth()}
      >
        <i className={`ti ${checking ? "ti-loader" : "ti-activity"}`} />
      </button>
      <button
        className="iconbtn"
        id="langBtn"
        style={{ fontSize: 13, fontWeight: 600 }}
        title={t("switch_lang")}
        aria-label={t("switch_lang")}
        onClick={toggleLang}
      >
        {LANG === "en" ? "中" : "EN"}
      </button>
      <button
        className="iconbtn"
        id="themeBtn"
        title={t("toggle_theme")}
        aria-label={t("toggle_theme")}
        onClick={toggleTheme}
      >
        <i className={`ti ${theme === "dark" ? "ti-sun" : "ti-moon"}`} />
      </button>
      {wsOpen && <WorkspaceModal onClose={() => setWsOpen(false)} />}
      {updOpen && updateInfo && <UpdatePanel info={updateInfo} onClose={() => setUpdOpen(false)} />}
    </header>
  );
}
