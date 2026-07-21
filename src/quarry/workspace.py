"""Quarry workspace(s) — where connections + saved queries live.

A *workspace* is a directory holding:
    connections.toml          - DB connections registry
    queries/<db>/<name>.sql   - saved named queries

The engine is generic and ships with NO workspace. Each org/project plugs in
its own. Multiple workspaces aggregate into one view (each keeps its own
`group`); manage the list with `qy workspace add|remove|list`.

Resolution order for the workspace root(s):
    1. explicit --workspace (os.pathsep-separated) / configure_workspace(...)
    2. ~/.config/quarry/config.toml  ->  workspaces = [...]
    3. current working directory

No environment variable feeds the workspace list — config.toml is the single
source of truth, so a stale shell export can never silently drop a workspace.
($QUARRY_CONFIG may relocate the config file itself.)

The FIRST workspace is the "primary" — mutations (connections add/set/remove,
query save) and psql/paths resolve against it. On a key/name conflict across
workspaces, the earlier workspace wins.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Workspace:
    home: Path
    connections_file: Path
    queries_dir: Path
    psql_bin: str


def _config_path() -> Path:
    return Path(os.environ.get("QUARRY_CONFIG")
                or (Path.home() / ".config" / "quarry" / "config.toml")).expanduser()


def _read_config() -> dict:
    """The full parsed config.toml (all top-level keys), or {} if absent/malformed."""
    p = _config_path()
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _dirs_from_config() -> list[Path]:
    """Persistent list of workspaces, read fresh every run (terminal-independent).

        # ~/.config/quarry/config.toml
        workspaces = ["~/.config/quarry", "~/workspace/.../dbq"]
    """
    ws = _read_config().get("workspaces") or []
    return [Path(str(x)).expanduser().resolve() for x in ws if str(x).strip()]


def config_workspaces() -> list[str]:
    """Raw workspace paths listed in config.toml (as written, unexpanded)."""
    return [str(x) for x in (_read_config().get("workspaces") or [])]


def _is_preservable_config_value(v: object) -> bool:
    if isinstance(v, (str, int, float, bool)):
        return True
    return isinstance(v, list) and all(isinstance(i, (str, int, float, bool)) for i in v)


def _toml_config_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_config_value(i) for i in v) + "]"
    esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


def _write_config(cfg: dict) -> Path:
    """Rewrite config.toml from a full key->value dict, read-modify-write style —
    any key besides the one(s) a caller is updating (e.g. `workspaces` vs
    `proxy_enabled_workspaces`) is preserved as-is instead of being dropped."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Quarry 配置 —— qy 每次读这里决定加载哪些 workspace(与终端环境变量无关)。",
        "# 管理:qy workspace add|remove <dir> / qy workspace list ; qy proxy on|off",
    ]
    for k, v in cfg.items():
        if not _is_preservable_config_value(v):
            print(f"warning: config.toml key '{k}' has an unsupported type and will be "
                  "dropped if this file is rewritten", file=sys.stderr)
            continue
        if k == "workspaces" and isinstance(v, list):
            # keep the historical pretty multi-line list format for `workspaces`
            lines.append("workspaces = [")
            for item in v:
                esc = str(item).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'  "{esc}",')
            lines.append("]")
        else:
            lines.append(f"{k} = {_toml_config_value(v)}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_config_workspaces(dirs: list[str]) -> Path:
    cfg = _read_config()
    cfg["workspaces"] = dirs
    return _write_config(cfg)


def _resolved(d: str) -> str:
    return str(Path(d).expanduser().resolve())


def add_workspace(d: str) -> tuple[bool, Path]:
    """Add a dir to config.toml (dedup by resolved path). Returns (added, config_path)."""
    ws = config_workspaces()
    if _resolved(d) in {_resolved(x) for x in ws}:
        return (False, _config_path())
    ws.append(d)
    return (True, _write_config_workspaces(ws))


def remove_workspace(d: str) -> bool:
    ws = config_workspaces()
    new = [x for x in ws if _resolved(x) != _resolved(d)]
    if len(new) == len(ws):
        return False
    _write_config_workspaces(new)
    return True


# ---------------------------------------------------------------------------
# Proxy toggle (issue #96) — workspace-granularity, stored alongside `workspaces`
# in config.toml so it survives across machines/terminals like everything else
# here. connections.toml is never touched by this.
# ---------------------------------------------------------------------------

_PROXY_KEY = "proxy_enabled_workspaces"


def proxy_enabled_workspaces() -> list[str]:
    """Raw (unresolved) workspace dirs with the proxy toggle on."""
    return [str(x) for x in (_read_config().get(_PROXY_KEY) or [])]


def is_proxy_enabled(ws_home: "str | Path") -> bool:
    target = _resolved(str(ws_home))
    return target in {_resolved(x) for x in proxy_enabled_workspaces()}


def set_proxy_enabled(ws_home: str, enabled: bool) -> Path:
    """Flip the proxy toggle for one workspace dir; returns the config path written."""
    cfg = _read_config()
    current = [str(x) for x in (cfg.get(_PROXY_KEY) or [])]
    target = _resolved(ws_home)
    without_target = [x for x in current if _resolved(x) != target]
    cfg[_PROXY_KEY] = (without_target + [ws_home]) if enabled else without_target
    return _write_config(cfg)


def _split_dirs(explicit: str | None) -> list[Path]:
    # Precedence: --workspace (explicit, os.pathsep-separated) > config.toml > cwd.
    # No environment variable on purpose — a stale shell export can't silently drop
    # workspaces. config.toml is the single source of truth (manage via `qy workspace`).
    if explicit:
        parts = [p for p in explicit.split(os.pathsep) if p.strip()]
        if parts:
            return [Path(p).expanduser().resolve() for p in parts]
    cfg_dirs = _dirs_from_config()
    if cfg_dirs:
        return cfg_dirs
    return [Path.cwd()]


def build_workspaces(explicit: str | None = None) -> list[Workspace]:
    dirs = _split_dirs(explicit)
    psql = os.environ.get("QUARRY_PSQL", "psql")
    cfile_override = os.environ.get("QUARRY_CONNECTIONS_FILE")
    qdir_override = os.environ.get("QUARRY_QUERIES_DIR")
    out: list[Workspace] = []
    for i, d in enumerate(dirs):
        cf = Path(cfile_override).expanduser() if (i == 0 and cfile_override) else d / "connections.toml"
        qd = Path(qdir_override).expanduser() if (i == 0 and qdir_override) else d / "queries"
        out.append(Workspace(home=d, connections_file=cf, queries_dir=qd, psql_bin=psql))
    return out


# Module-global current workspace(s). WS is the primary; WS_LIST is all of them.
# _EXPLICIT remembers whatever `explicit` value was last handed to
# configure_workspace() (e.g. a `--workspace` CLI flag), so long-lived processes
# (the GUI server) can re-resolve config.toml changes via reload_workspace()
# without accidentally discarding that override.
WS_LIST: list[Workspace] = build_workspaces()
WS: Workspace = WS_LIST[0]
_EXPLICIT: str | None = None


def configure_workspace(explicit: str | None = None) -> Workspace:
    global WS, WS_LIST, _EXPLICIT
    _EXPLICIT = explicit
    WS_LIST = build_workspaces(explicit)
    WS = WS_LIST[0]
    return WS


def reload_workspace() -> Workspace:
    """Re-resolve WS/WS_LIST against config.toml, preserving whatever explicit
    --workspace override (if any) the process started with. Use this instead of
    configure_workspace(None) whenever the reload is triggered by a config.toml
    edit from a long-lived process (e.g. the GUI's workspace-manager add/remove),
    so an explicit --workspace session doesn't get silently dropped."""
    return configure_workspace(_EXPLICIT)
