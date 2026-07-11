import { useRef, useState } from "react";
import { t } from "./i18n";
import { tabTitle, useTabsStore, type Tab } from "./store/tabsStore";

export type TabBarProps = {
  /** Called instead of the store's own switch so the caller can re-point the
   * connection/editor to the target tab's db/env first. */
  onSwitch: (tab: Tab) => void;
  /** Called instead of the store's own close so the caller can stash the
   * dying tab's SQL into History first (never silently lost). */
  onClose: (tab: Tab) => void;
};

/** The editor tab bar — legacy DOM: `.tabs > .tab[data-i] > .lbl/.x` plus the
 * dashed `#tabAdd` button; double-click renames in place, drag reorders,
 * middle-click closes. */
export default function TabBar({ onSwitch, onClose }: TabBarProps) {
  const tabs = useTabsStore((s) => s.tabs);
  const activeId = useTabsStore((s) => s.activeId);
  const addTab = useTabsStore((s) => s.addTab);
  const renameTab = useTabsStore((s) => s.renameTab);
  const reorderTab = useTabsStore((s) => s.reorderTab);

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  const renameCommittedRef = useRef(false);

  const startRename = (tab: Tab): void => {
    renameCommittedRef.current = false;
    setDraftTitle(tabTitle(tab));
    setRenamingId(tab.id);
  };

  const commitRename = (revert: boolean): void => {
    if (renameCommittedRef.current || renamingId === null) return;
    renameCommittedRef.current = true;
    // An empty name reverts the tab to its automatic db@env / SQL title.
    if (!revert) renameTab(renamingId, draftTitle.trim() || null);
    setRenamingId(null);
  };

  return (
    <div className="tabs" id="tabs">
      {tabs.map((tab, i) => (
        <span
          key={tab.id}
          className={`tab${tab.id === activeId ? " on" : ""}${renamingId === tab.id ? " renaming" : ""}${dragId === tab.id ? " dragging" : ""}${dragOverId === tab.id ? " dragover" : ""}`}
          data-i={i}
          title={tab.sql.slice(0, 300)}
          draggable
          onClick={() => {
            if (renamingId === tab.id) return;
            if (tab.id !== activeId) onSwitch(tab);
          }}
          onDoubleClick={(e) => {
            e.stopPropagation();
            startRename(tab);
          }}
          onMouseDown={(e) => {
            if (e.button === 1) e.preventDefault(); // no browser middle-click autoscroll
          }}
          onAuxClick={(e) => {
            if (e.button === 1 && tabs.length > 1) {
              e.preventDefault();
              onClose(tab);
            }
          }}
          onDragStart={(e) => {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", tab.id);
            setDragId(tab.id);
          }}
          onDragEnd={() => {
            setDragId(null);
            setDragOverId(null);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            setDragOverId(tab.id);
          }}
          onDragLeave={() => setDragOverId((v) => (v === tab.id ? null : v))}
          onDrop={(e) => {
            e.preventDefault();
            setDragOverId(null);
            const fromId = e.dataTransfer.getData("text/plain");
            if (fromId) reorderTab(fromId, tab.id);
          }}
        >
          {renamingId === tab.id ? (
            <input
              className="rn"
              autoFocus
              maxLength={60}
              value={draftTitle}
              onClick={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
              onChange={(e) => setDraftTitle(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") commitRename(false);
                else if (e.key === "Escape") commitRename(true);
              }}
              onBlur={() => commitRename(false)}
            />
          ) : (
            <span className="lbl">{tabTitle(tab)}</span>
          )}
          {tabs.length > 1 && (
            <span
              className="x"
              data-x={i}
              title={t("close_tab")}
              onClick={(e) => {
                e.stopPropagation();
                onClose(tab);
              }}
            >
              ×
            </span>
          )}
        </span>
      ))}
      <span className="tab add" id="tabAdd" title={t("new_tab")} onClick={() => addTab()}>
        +
      </span>
    </div>
  );
}
