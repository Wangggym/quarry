import { useRef, useState } from "react";
import { tabTitle, useTabsStore } from "./store/tabsStore";
import type { Tab } from "./store/types";

export type TabBarProps = {
  /** Called before the store switches the active tab, so the caller can
   * re-point the connection/editor to the target tab's db/env. */
  onSwitch: (tab: Tab) => void;
  /** Called instead of the store's own `closeTab` so the caller can stash
   * the dying tab's SQL into History first (never silently lost). */
  onClose: (tab: Tab) => void;
};

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
    if (!revert) renameTab(renamingId, draftTitle.trim() || null);
    setRenamingId(null);
  };

  return (
    <div className="tab-bar" id="react-tabs" role="tablist">
      {tabs.map((tab) => (
        <span
          key={tab.id}
          className={`tab${tab.id === activeId ? " on" : ""}${renamingId === tab.id ? " renaming" : ""}${dragId === tab.id ? " dragging" : ""}${dragOverId === tab.id ? " dragover" : ""}`}
          data-testid="tab"
          data-tab-id={tab.id}
          title={tab.sql.slice(0, 300)}
          draggable
          role="tab"
          aria-selected={tab.id === activeId}
          onClick={() => {
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
              data-testid="tab-rename-input"
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
              data-testid="tab-close"
              title="Close tab"
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
      <button type="button" className="tab add" id="react-tab-add" title="New tab" onClick={() => addTab()}>
        +
      </button>
    </div>
  );
}
