import { useEffect, useState } from "react";
import { addWorkspace, fetchWorkspaces, removeWorkspace, type WorkspacesResponse } from "./api";
import { useUiStore } from "./store/uiStore";
import { t } from "./i18n";

type Props = { onClose: () => void };

/** config.toml-registered workspace dirs, add/remove without leaving the GUI
 * — React port of the legacy `openWorkspaces()`/`renderWorkspaces()`. */
export default function WorkspaceModal({ onClose }: Props) {
  const lang = useUiStore((s) => s.lang);
  const [data, setData] = useState<WorkspacesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");

  const load = (): void => {
    fetchWorkspaces()
      .then(setData)
      .catch((e) => setError(String(e.message ?? e)));
  };

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const doAdd = async (): Promise<void> => {
    const dir = input.trim();
    if (!dir) return;
    try {
      const next = await addWorkspace(dir);
      setData(next);
      setInput("");
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  };

  const doRemove = async (dir: string): Promise<void> => {
    if (!window.confirm(`Remove workspace ${dir}?`)) return;
    try {
      const next = await removeWorkspace(dir);
      setData(next);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  };

  return (
    <div id="react-workspace-backdrop" onClick={onClose}>
      <div id="react-workspace-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>{t(lang, "manageWorkspaces")}</strong>
          <button type="button" onClick={onClose}>
            {t(lang, "close")}
          </button>
        </div>
        <div className="modal-body">
          {error && <p className="grid-error">{error}</p>}
          {!data && <p>loading…</p>}
          {data && (
            <>
              {data.items.length === 0 && <p className="ws-empty">No workspaces registered.</p>}
              <ul className="ws-list">
                {data.items.map((it) => (
                  <li key={it.dir} className="ws-row" data-testid="ws-row" data-dir={it.dir}>
                    <span className="ws-path" title={it.dir}>
                      {it.display}
                    </span>
                    {!it.exists && <span className="ws-warn">missing</span>}
                    {it.exists && !it.hasConnections && <span className="ws-warn">no connections</span>}
                    <button
                      type="button"
                      className="ws-remove"
                      data-testid="ws-remove"
                      onClick={() => void doRemove(it.dir)}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
              <div className="ws-add">
                <input
                  id="react-workspace-input"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void doAdd();
                  }}
                  placeholder="/path/to/workspace"
                />
                <button type="button" id="react-workspace-add-btn" onClick={() => void doAdd()}>
                  {t(lang, "add")}
                </button>
              </div>
              <div className="ws-config">config: {data.config}</div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
