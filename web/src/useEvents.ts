import { useEffect } from "react";
import { fetchUpdate, fetchVersion } from "./api";
import { t } from "./i18n";
import { useConnStore } from "./store/connStore";
import { toast } from "./store/toastStore";
import { useUiStore } from "./store/uiStore";

function refreshUpdateInfo(): void {
  fetchUpdate()
    .then((u) => useUiStore.getState().setUpdateInfo(u))
    .catch(() => {});
}

/** Subscribe to the backend's `/api/events` SSE channel (see the Events
 * contract in `src/quarry/gui.py`). Events are refetch *hints*, never data:
 *
 * - `workspace_changed` → bump `reloadToken` (one bump refetches both the
 *   connection tree and the saved-query list) + a confirmation toast.
 * - `update_available` → refetch `/api/update` so the header badge picks up
 *   the newer PyPI release the backend just found.
 * - EventSource reconnect after an error → the server restarted (or the
 *   network blipped): re-read `/api/version`; a changed version means the
 *   user upgraded Quarry, so raise the "reload page" banner. Data may also
 *   have changed while we were disconnected, so refetch regardless.
 */
export function useEvents(): void {
  useEffect(() => {
    let baseVersion: string | null = null;
    let wasDown = false;
    let closed = false;
    fetchVersion()
      .then((v) => {
        baseVersion = v.version;
      })
      .catch(() => {});
    refreshUpdateInfo();
    const es = new EventSource("/api/events");
    es.onerror = () => {
      wasDown = true;
    };
    es.onopen = () => {
      if (!wasDown || closed) return;
      wasDown = false;
      useConnStore.getState().requestReload();
      fetchVersion()
        .then((v) => {
          if (baseVersion !== null && v.version !== baseVersion)
            useUiStore.getState().setUpgradedTo(v.version);
        })
        .catch(() => {});
    };
    es.onmessage = (m) => {
      let type: string | undefined;
      try {
        type = (JSON.parse(m.data) as { type?: string }).type;
      } catch {
        return; // malformed event — ignore, next one will hint again
      }
      if (type === "workspace_changed") {
        useConnStore.getState().requestReload();
        toast(t("ws_files_changed"), true);
      } else if (type === "update_available") {
        refreshUpdateInfo();
      }
    };
    return () => {
      closed = true;
      es.close();
    };
  }, []);
}
