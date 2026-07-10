import { useEffect, useState } from "react";
import { addWorkspace, fetchWorkspaces, removeWorkspace, type WorkspacesResponse } from "./api";
import { t, tv } from "./i18n";
import { useModalEscape } from "./modalStack";
import { useConnStore } from "./store/connStore";
import { toast } from "./store/toastStore";

type Props = { onClose: () => void };

/** config.toml-registered workspace dirs: list (with missing-dir /
 * no-connections warnings), add, and confirm-gated remove — legacy
 * `openWorkspaces()` DOM (`.wslist/.wsrow/.wspath/.wswarn/.wsadd/.wsconfig`).
 * Every change refreshes the connection tree immediately. */
export default function WorkspaceModal({ onClose }: Props) {
  const [data, setData] = useState<WorkspacesResponse | null>(null);
  const [input, setInput] = useState("");

  useModalEscape(onClose);

  useEffect(() => {
    fetchWorkspaces()
      .then(setData)
      .catch((e) => toast(String((e as Error).message ?? e), false));
  }, []);

  const doAdd = async (): Promise<void> => {
    const dir = input.trim();
    if (!dir) return;
    try {
      const next = await addWorkspace(dir);
      setData(next);
      setInput("");
      toast(t("ws_added"), true);
      // A newly registered workspace may bring its own connections.
      useConnStore.getState().requestReload();
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
    }
  };

  const doRemove = async (dir: string): Promise<void> => {
    if (!window.confirm(tv("ws_remove_confirm", { dir }))) return;
    try {
      const next = await removeWorkspace(dir);
      setData(next);
      toast(t("ws_removed"), true);
      // The removed workspace's connections must disappear immediately —
      // including unbinding the active connection if it belonged to it.
      useConnStore.getState().requestReload();
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
    }
  };

  return (
    <div className="modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="box" id="wsbox" style={{ width: "min(520px, 85%)" }}>
        <div className="mh">
          <i className="ti ti-stack-2" /> {t("ws_title")}
        </div>
        <div id="wsbody">
          {!data && (
            <div className="spin">
              <i className="ti ti-loader" />
            </div>
          )}
          {data && (
            <>
              {data.items.length === 0 ? (
                <p style={{ color: "var(--fg3)", fontSize: "12.5px", margin: "0 0 8px" }}>
                  {t("ws_empty")}
                </p>
              ) : (
                <div className="wslist">
                  {data.items.map((it) => {
                    const warn = !it.exists
                      ? t("ws_missing")
                      : !it.hasConnections
                        ? t("ws_no_conn")
                        : "";
                    return (
                      <div className="wsrow" key={it.dir}>
                        <span className="wspath" title={it.dir}>
                          {it.display}
                        </span>
                        {warn && <span className="wswarn">{warn}</span>}
                        <button
                          className="iconbtn wsdel"
                          data-dir={it.dir}
                          title={t("ws_remove")}
                          onClick={() => void doRemove(it.dir)}
                        >
                          <i className="ti ti-trash" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
              <div className="wsadd">
                <input
                  id="wsInput"
                  placeholder={t("ws_add_ph")}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void doAdd()}
                />
                <button className="btn" id="wsAddBtn" onClick={() => void doAdd()}>
                  <i className="ti ti-plus" /> {t("ws_add")}
                </button>
              </div>
              <div className="wsconfig">
                {t("ws_config")}: {data.config}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
