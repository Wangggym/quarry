import { create } from "zustand";
import type { ConnGroup } from "../api";

export type CurrentConn = { db: string; env: string | null; engine: string } | null;

type ConnMetaState = {
  workspace: string;
  workspaces: string[];
  groups: ConnGroup[];
  current: CurrentConn;
  setConnMeta: (workspace: string, workspaces: string[], groups: ConnGroup[]) => void;
  setCurrent: (current: CurrentConn) => void;
};

/** Header-only slice of the connection state ResultWorkbench already owns —
 * mirrored here (not moved) so `Header.tsx` can render the workspace label,
 * prod badge, and connection-info button without ResultWorkbench handing
 * down request-tracking internals it doesn't need. */
export const useConnMetaStore = create<ConnMetaState>((set) => ({
  workspace: "",
  workspaces: [],
  groups: [],
  current: null,
  setConnMeta: (workspace, workspaces, groups) => set({ workspace, workspaces, groups }),
  setCurrent: (current) => set({ current }),
}));
