import { useEffect, useMemo, useRef, useState } from "react";
import type { QueryColumn, SavedQuery } from "./api";
import { copy } from "./clip";
import { t } from "./i18n";
import { useModalEscape } from "./modalStack";
import type { HistEntry } from "./useSqlHistory";

type Row = Record<string, unknown>;

export function cellText(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "object") return JSON.stringify(v); // jsonb / arrays
  return String(v);
}

/** Backdrop + box wrapper shared by every modal — legacy `.modal > .box`. */
function Modal({
  onClose,
  boxStyle,
  boxId,
  children,
}: {
  onClose: () => void;
  boxStyle?: React.CSSProperties;
  boxId?: string;
  children: React.ReactNode;
}) {
  useModalEscape(onClose);
  return (
    <div className="modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="box" id={boxId} style={boxStyle}>
        {children}
      </div>
    </div>
  );
}

/* ---- collapsible JSON tree (cell modal), legacy .jt/.jrow markup ---- */

function JsonTree({ value, jsonKey }: { value: unknown; jsonKey?: string | number }) {
  const k = jsonKey !== undefined ? (
    <>
      <span className="jk">{String(jsonKey)}</span>:{" "}
    </>
  ) : null;
  if (value === null) {
    return (
      <div className="jrow">
        {k}
        <span className="jnull">null</span>
      </div>
    );
  }
  if (Array.isArray(value)) {
    if (!value.length) return <div className="jrow">{k}[]</div>;
    return (
      <details className="jt" open>
        <summary>
          {k}
          <span className="jm">[{value.length}]</span>
        </summary>
        {value.map((x, i) => (
          <JsonTree key={i} value={x} jsonKey={i} />
        ))}
      </details>
    );
  }
  if (typeof value === "object") {
    const keys = Object.keys(value as object);
    if (!keys.length) return <div className="jrow">{k}{"{}"}</div>;
    return (
      <details className="jt" open>
        <summary>
          {k}
          <span className="jm">{`{${keys.length}}`}</span>
        </summary>
        {keys.map((kk) => (
          <JsonTree key={kk} value={(value as Row)[kk]} jsonKey={kk} />
        ))}
      </details>
    );
  }
  const cls = typeof value === "number" ? "jnum" : typeof value === "boolean" ? "jbool" : "jstr";
  return (
    <div className="jrow">
      {k}
      <span className={cls}>
        {typeof value === "string" ? JSON.stringify(value) : String(value)}
      </span>
    </div>
  );
}

/** Cell-value modal: JSON renders as a collapsible tree, anything else as
 * preformatted text; the header offers a one-click Copy. */
export function CellModal({ value, onClose }: { value: string; onClose: () => void }) {
  const parsed = useMemo(() => {
    try {
      const p = JSON.parse(value);
      return p && typeof p === "object" ? p : null;
    } catch {
      return null;
    }
  }, [value]);
  return (
    <Modal onClose={onClose} boxStyle={{ minWidth: "min(560px, 80vw)" }}>
      <div className="mh">
        <i className="ti ti-eye" /> {t("cell")}{" "}
        <span
          id="cpy"
          style={{ cursor: "pointer", color: "var(--accent)" }}
          onClick={() => {
            copy(value);
            onClose();
          }}
        >
          {t("copy")}
        </span>
      </div>
      {parsed !== null ? <JsonTree value={parsed} /> : <pre>{value}</pre>}
    </Modal>
  );
}

/** Whole-row detail modal (opened from the row-number cell). */
export function RowDetailModal({
  row,
  columns,
  onClose,
}: {
  row: Row;
  columns: QueryColumn[];
  onClose: () => void;
}) {
  return (
    <Modal onClose={onClose} boxStyle={{ width: "60%" }}>
      <div className="mh">
        <i className="ti ti-list-details" /> {t("row_detail")}
      </div>
      <table style={{ border: 0, width: "100%" }}>
        <tbody>
          {columns.map((c) => {
            const text = cellText(row[c.name]);
            return (
              <tr key={c.name}>
                <td
                  style={{
                    color: "var(--fg2)",
                    padding: "4px 12px 4px 0",
                    verticalAlign: "top",
                    whiteSpace: "nowrap",
                  }}
                >
                  {c.name}
                  {c.type && (
                    <>
                      {" "}
                      <span className="ty" style={{ color: "var(--fg3)" }}>
                        {c.type}
                      </span>
                    </>
                  )}
                </td>
                <td style={{ padding: "4px 0", wordBreak: "break-word", fontFamily: "var(--mono)" }}>
                  {text === null ? (
                    <span style={{ color: "var(--null)", fontStyle: "italic" }}>NULL</span>
                  ) : (
                    text
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Modal>
  );
}

/** EXPLAIN plan modal (single-column plans; tabular plans go to the grid). */
export function ExplainModal({
  plan,
  db,
  env,
  onClose,
}: {
  plan: string;
  db: string;
  env: string | null;
  onClose: () => void;
}) {
  return (
    <Modal onClose={onClose} boxStyle={{ minWidth: "min(760px, 85vw)" }}>
      <div className="mh">
        <i className="ti ti-route" /> EXPLAIN · {db}
        {env ? `@${env}` : ""}
      </div>
      <pre>{plan}</pre>
    </Modal>
  );
}

function fmtAgo(ts: number): string {
  if (!ts) return "";
  const s = (Date.now() - ts) / 1000;
  return s < 60
    ? t("just_now")
    : s < 3600
      ? Math.floor(s / 60) + t("min_ago")
      : s < 86400
        ? Math.floor(s / 3600) + t("hr_ago")
        : Math.floor(s / 86400) + t("day_ago");
}

/** Query-history modal: search box + recallable entries with db@env / age
 * metadata (legacy `.hsearch/.hitem/.hmeta`). */
export function HistoryModal({
  history,
  onRecall,
  onClose,
}: {
  history: HistEntry[];
  onRecall: (sql: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return history;
    return history.filter(
      (h) =>
        h.sql.toLowerCase().includes(needle) || (h.db ?? "").toLowerCase().includes(needle),
    );
  }, [history, q]);

  return (
    <Modal onClose={onClose} boxStyle={{ width: "min(680px, 80%)" }}>
      <div className="mh">
        <i className="ti ti-history" /> {t("hist_title")} · {history.length}
      </div>
      <input
        className="hsearch"
        autoFocus
        placeholder={t("hist_search")}
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div id="hlist">
        {shown.length === 0 && <div className="empty">{t("no_match")}</div>}
        {shown.map((h, i) => {
          const meta = [h.db ? h.db + (h.env ? `@${h.env}` : "") : "", fmtAgo(h.ts)]
            .filter(Boolean)
            .join(" · ");
          return (
            <div
              key={i}
              className="hitem"
              style={{ cursor: "pointer", padding: "7px 6px", borderBottom: "1px solid var(--line)" }}
              onClick={() => onRecall(h.sql)}
            >
              <pre
                style={{
                  margin: 0,
                  fontFamily: "var(--mono)",
                  fontSize: "12.5px",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {h.sql}
              </pre>
              {meta && <div className="hmeta">{meta}</div>}
            </div>
          );
        })}
      </div>
    </Modal>
  );
}

/** Saved-query parameter modal: required/default hints, Enter submits,
 * click-out closes (legacy `.pf` fields + `#pgo`). */
export function ParamModal({
  query,
  onClose,
  onSubmit,
}: {
  query: SavedQuery;
  onClose: () => void;
  onSubmit: (params: Record<string, string>) => void;
}) {
  const boxRef = useRef<HTMLDivElement | null>(null);

  const submit = (): void => {
    const params: Record<string, string> = {};
    boxRef.current?.querySelectorAll<HTMLInputElement>(".pf").forEach((el) => {
      if (el.value !== "") params[el.dataset.p as string] = el.value;
    });
    onClose();
    onSubmit(params);
  };

  useModalEscape(onClose);
  useEffect(() => {
    const first = boxRef.current?.querySelector<HTMLInputElement>(".pf");
    first?.focus();
    first?.select();
  }, []);

  return (
    <div
      className="modal"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      onKeyDown={(e) => e.key === "Enter" && submit()}
    >
      <div className="box" ref={boxRef} style={{ width: "min(460px, 80%)" }}>
        <div className="mh">
          <i className="ti ti-adjustments" /> {query.name} · {t("fill_params")}
        </div>
        {query.desc && (
          <div style={{ color: "var(--fg3)", fontSize: "11.5px", marginBottom: 6 }}>
            {query.desc}
          </div>
        )}
        {query.params.map((p) => (
          <div key={p.name} style={{ margin: "8px 0" }}>
            <label
              style={{
                fontSize: 12,
                color: "var(--fg2)",
                display: "block",
                marginBottom: 3,
              }}
            >
              {p.name} <span style={{ color: "var(--fg3)" }}>{p.type || "text"}</span>
              {p.required ? (
                <span style={{ color: "var(--red-fg)" }}> {t("required")}</span>
              ) : p.default != null ? (
                <span style={{ color: "var(--fg3)" }}>
                  {" "}
                  {t("default_v")} {String(p.default)}
                </span>
              ) : null}
            </label>
            <input
              className="pf"
              data-p={p.name}
              defaultValue={p.default != null ? String(p.default) : ""}
              placeholder={p.name}
              style={{
                width: "100%",
                background: "var(--bg2)",
                border: "1px solid var(--line2)",
                borderRadius: 6,
                color: "var(--fg)",
                padding: "6px 9px",
                fontFamily: "var(--mono)",
                fontSize: "12.5px",
              }}
            />
          </div>
        ))}
        <div style={{ textAlign: "right", marginTop: 12 }}>
          <button className="btn primary" id="pgo" onClick={submit}>
            <i className="ti ti-player-play" /> {t("run")}
          </button>
        </div>
      </div>
    </div>
  );
}
