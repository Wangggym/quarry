import { useEffect, useState } from "react";
import { fetchConnInfo, fetchHealth, localSync, localUp, type ConnInfo, type HealthResponse } from "./api";
import { useConnMetaStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";
import { t } from "./i18n";

type Props = { db: string; env: string | null; onClose: () => void };

/** Resolved-connection details for the active tab's target: what quarry will
 * actually dial, and a live reachability probe — mirrors the legacy GUI's
 * `openConnInfo()`. The URL defaults to masked (see `_mask_url` in gui.py)
 * and is only ever fetched in the clear via an explicit reveal/copy click —
 * this must never regress into eagerly fetching the plaintext URL. */
export default function ConnInfoModal({ db, env, onClose }: Props) {
  const lang = useUiStore((s) => s.lang);
  const groups = useConnMetaStore((s) => s.groups);
  const [info, setInfo] = useState<ConnInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthResponse>({ ok: null });
  const [upBusy, setUpBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setInfo(null);
    setError(null);
    setRevealed(false);
    setHealth({ ok: null });
    setActionMsg(null);
    fetchConnInfo(db, env)
      .then((d) => {
        if (cancelled) return;
        setInfo(d);
        setDisplayUrl(d.url);
      })
      .catch((e) => !cancelled && setError(String(e.message ?? e)));
    fetchHealth(db, env, { fresh: true })
      .then((h) => !cancelled && setHealth(h))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [db, env]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const item = groups.flatMap((g) => g.items).find((it) => it.db === db);
  const envs = item?.envs ?? [];
  const hasLocal = envs.some((e) => e.env === "local");
  const isLocal = (env ?? "").toLowerCase() === "local";
  const srcEnv =
    envs.find((e) => e.env === "dev")?.env ?? envs.find((e) => e.env && e.env !== "local")?.env ?? "dev";

  const toggleReveal = async (): Promise<void> => {
    const next = !revealed;
    if (next) {
      const d = await fetchConnInfo(db, env, { reveal: true });
      setDisplayUrl(d.url);
    } else {
      setDisplayUrl(info?.url ?? null);
    }
    setRevealed(next);
  };

  const copyUrl = async (): Promise<void> => {
    const d = await fetchConnInfo(db, env, { reveal: true });
    await navigator.clipboard.writeText(d.url);
  };

  const createLocal = async (): Promise<void> => {
    setUpBusy(true);
    setActionMsg(null);
    try {
      const r = await localUp(db);
      setActionMsg(
        r.sync_error
          ? `created, sync failed: ${r.sync_error}`
          : r.synced_from
            ? `created + synced from ${r.synced_from}`
            : "created",
      );
    } catch (e) {
      setActionMsg(String((e as Error).message ?? e));
    } finally {
      setUpBusy(false);
    }
  };

  const syncSchema = async (): Promise<void> => {
    if (!window.confirm(`Sync schema from ${srcEnv}? This replaces the local database.`)) return;
    setSyncBusy(true);
    setActionMsg(null);
    try {
      await localSync(db, srcEnv);
      setActionMsg(`synced from ${srcEnv}`);
    } catch (e) {
      setActionMsg(String((e as Error).message ?? e));
    } finally {
      setSyncBusy(false);
    }
  };

  return (
    <div id="react-conninfo-backdrop" onClick={onClose}>
      <div id="react-conninfo-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>
            {t(lang, "connInfo")} · {db}
            {env ? `@${env}` : ""}
          </strong>
          <button type="button" onClick={onClose}>
            {t(lang, "close")}
          </button>
        </div>
        <div className="modal-body">
          {error && <p className="grid-error">{error}</p>}
          {info && (
            <>
              <div className="ci-row">
                <span className="ci-k">engine</span>
                <span className="ci-v">{info.engine}</span>
              </div>
              <div className="ci-row">
                <span className="ci-k">host</span>
                <span className="ci-v">{info.host}</span>
              </div>
              <div className="ci-row">
                <span className="ci-k">port</span>
                <span className="ci-v">{info.port}</span>
              </div>
              <div className="ci-row">
                <span className="ci-k">database</span>
                <span className="ci-v">{info.database}</span>
              </div>
              <div className="ci-row">
                <span className="ci-k">url</span>
                <span className="ci-v" id="react-conninfo-url" data-testid="conninfo-url">
                  {displayUrl}
                </span>
                <button type="button" id="react-conninfo-reveal" onClick={() => void toggleReveal()}>
                  {revealed ? t(lang, "hide") : t(lang, "reveal")}
                </button>
                <button type="button" id="react-conninfo-copy" onClick={() => void copyUrl()}>
                  {t(lang, "copy")}
                </button>
              </div>
              {info.file && (
                <div className="ci-row">
                  <span className="ci-k">file</span>
                  <span className="ci-v">{info.file}</span>
                </div>
              )}
              <div
                id="react-conninfo-health"
                className={`ci-health ${health.ok === true ? "ok" : health.ok === false ? "down" : ""}`}
              >
                {health.ok === null ? "checking…" : health.ok ? "reachable" : `unreachable${health.error ? `: ${health.error}` : ""}`}
              </div>
              {!hasLocal && (info.engine === "postgres" || info.engine === "redis") && (
                <button type="button" id="react-conninfo-mklocal" disabled={upBusy} onClick={() => void createLocal()}>
                  {upBusy ? t(lang, "running") : t(lang, "createLocalEnv")}
                </button>
              )}
              {isLocal && info.engine === "postgres" && (
                <button type="button" id="react-conninfo-sync" disabled={syncBusy} onClick={() => void syncSchema()}>
                  {syncBusy ? t(lang, "running") : t(lang, "syncSchemaFrom", { env: srcEnv })}
                </button>
              )}
              {actionMsg && <p className="ci-action-msg" data-testid="conninfo-action-msg">{actionMsg}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
