"""Local dev containers — `qy local up/down/status`.

Bring a Postgres/Redis instance up in a docker container so a locally-running
service talks only to `localhost` instead of a shared remote (dev) database. It
shells out to the `docker` CLI (same style as the `psql` / `redis-cli` calls
elsewhere — no new SDK dependency) and, on `up <key>`, auto-registers an
`env=local` connection into `connections.toml`.

Shared-container model: ONE Postgres container hosts many logical databases
(one per connection key); it is not a container-per-key. Data lives on a named
docker volume so it survives `down` (and `stop`/`start`) unless `--purge`.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

from . import core
from .core import EXIT_CONNECTION_ERROR, CONN_KEY_RE, QuarryError

LOCAL_ENV = "local"

# Credentials baked into the local Postgres container (localhost-only; the whole
# point of `qy local` is that nothing here is remotely reachable).
LOCAL_PG_USER = "quarry"
LOCAL_PG_PASSWORD = "quarry"

# A logical-db name doubles as a Postgres database name and a connection-key
# suffix, so it must be a safe SQL identifier — reuse the connection-key rule.
SAFE_DB_RE = CONN_KEY_RE


@dataclass(frozen=True)
class EngineSpec:
    engine: str
    container: str
    volume: str
    port: int          # host port (fixed convention)
    internal_port: int
    default_image: str

    def url(self, dbname: str) -> str:
        if self.engine == "postgres":
            return (f"postgresql://{LOCAL_PG_USER}:{LOCAL_PG_PASSWORD}"
                    f"@localhost:{self.port}/{dbname}")
        return f"redis://localhost:{self.port}/0"


PG_SPEC = EngineSpec(
    engine="postgres", container="quarry-local-postgres", volume="quarry-local-pgdata",
    port=5433, internal_port=5432, default_image="postgres:16-alpine",
)
REDIS_SPEC = EngineSpec(
    engine="redis", container="quarry-local-redis", volume="quarry-local-redisdata",
    port=6380, internal_port=6379, default_image="redis:7-alpine",
)
SPECS: dict[str, EngineSpec] = {"postgres": PG_SPEC, "redis": REDIS_SPEC}


def specs_for(engine: str | None) -> list[EngineSpec]:
    if engine in (None, "all"):
        return [PG_SPEC, REDIS_SPEC]
    return [SPECS[engine]]


# ---------------------------------------------------------------------------
# docker CLI seam — the single place a real docker subprocess is invoked
# ---------------------------------------------------------------------------

def resolve_docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise QuarryError(
            "docker not found in PATH — install Docker to use `qy local`",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return docker


def _run_docker(args: list[str], *, timeout: int = 60) -> tuple[int, str, str]:  # pragma: no cover - thin subprocess seam
    cmd = [resolve_docker(), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (-1, "", f"docker timed out after {timeout}s")
    return (proc.returncode, proc.stdout, proc.stderr)


def docker_available() -> bool:
    """True if the docker binary exists AND the daemon answers `docker version`."""
    if not shutil.which("docker"):
        return False
    try:
        rc, _, _ = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
    except Exception:
        return False
    return rc == 0


def require_docker() -> None:
    """Raise a readable QuarryError when docker is missing or the daemon is down."""
    if not shutil.which("docker"):
        raise QuarryError(
            "docker not found in PATH — install Docker to use `qy local`",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    rc, _, _ = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
    if rc != 0:
        raise QuarryError(
            "docker is installed but the daemon isn't responding (is Docker running?)",
            exit_code=EXIT_CONNECTION_ERROR,
        )


# ---------------------------------------------------------------------------
# container / volume / port inspection
# ---------------------------------------------------------------------------

def container_state(name: str) -> str:
    """One of: 'running' | 'stopped' | 'absent'."""
    rc, out, _ = _run_docker(["inspect", "-f", "{{.State.Running}}", name], timeout=15)
    if rc != 0:
        return "absent"
    return "running" if out.strip() == "true" else "stopped"


def container_image(name: str) -> str | None:
    rc, out, _ = _run_docker(["inspect", "-f", "{{.Config.Image}}", name], timeout=15)
    return out.strip() if rc == 0 and out.strip() else None


def volume_exists(name: str) -> bool:
    rc, _, _ = _run_docker(["volume", "inspect", name], timeout=15)
    return rc == 0


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------------------
# lifecycle: up / down
# ---------------------------------------------------------------------------

def _docker_run_args(spec: EngineSpec, image: str) -> list[str]:
    base = ["run", "-d", "--name", spec.container, "-p", f"{spec.port}:{spec.internal_port}"]
    if spec.engine == "postgres":
        return base + [
            "-e", f"POSTGRES_USER={LOCAL_PG_USER}",
            "-e", f"POSTGRES_PASSWORD={LOCAL_PG_PASSWORD}",
            "-e", "POSTGRES_DB=postgres",
            "-v", f"{spec.volume}:/var/lib/postgresql/data",
            image,
        ]
    return base + ["-v", f"{spec.volume}:/data", image]


def start_container(spec: EngineSpec, *, image: str | None = None) -> str:
    """Bring the container up idempotently. Returns 'running' (already up),
    'started' (a stopped container resumed), or 'created' (freshly run)."""
    require_docker()
    state = container_state(spec.container)
    if state == "running":
        return "running"
    if state == "stopped":
        rc, _, e = _run_docker(["start", spec.container], timeout=30)
        if rc != 0:
            raise QuarryError(
                f"failed to start container {spec.container}: {e.strip()}",
                exit_code=EXIT_CONNECTION_ERROR,
            )
        return "started"
    # absent -> create fresh; the host port must be free (only meaningful here,
    # since an already-running quarry container legitimately holds the port).
    if port_in_use(spec.port):
        raise QuarryError(
            f"port {spec.port} is already in use — free it or stop the "
            f"conflicting service before `qy local up`",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    rc, _, e = _run_docker(_docker_run_args(spec, image or spec.default_image), timeout=180)
    if rc != 0:
        raise QuarryError(
            f"failed to start local {spec.engine} container: {e.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return "created"


def wait_pg_ready(spec: EngineSpec, *, timeout: int = 40) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, _, _ = _run_docker(
            ["exec", spec.container, "pg_isready", "-U", LOCAL_PG_USER], timeout=10)
        if rc == 0:
            return True
        time.sleep(0.5)
    return False


def ensure_pg_database(spec: EngineSpec, dbname: str) -> None:
    """Create the logical database inside the shared Postgres container if absent."""
    if not SAFE_DB_RE.match(dbname):  # defensive: callers validate first
        raise QuarryError(f"invalid local database name '{dbname}'", exit_code=core.EXIT_USAGE)
    rc, out, _ = _run_docker(
        ["exec", spec.container, "psql", "-U", LOCAL_PG_USER, "-tAc",
         f"SELECT 1 FROM pg_database WHERE datname='{dbname}'"], timeout=15)
    if rc == 0 and out.strip() == "1":
        return
    rc, _, e = _run_docker(
        ["exec", spec.container, "createdb", "-U", LOCAL_PG_USER, dbname], timeout=30)
    if rc != 0 and "already exists" not in (e or "").lower():
        raise QuarryError(
            f"failed to create database '{dbname}': {e.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )


def down_engine(spec: EngineSpec, *, purge: bool) -> dict:
    """Stop the container. With purge=True also remove it and its data volume.
    Returns a summary dict for the CLI to render."""
    require_docker()
    state = container_state(spec.container)
    result = {"engine": spec.engine, "was": state, "stopped": False,
              "purged": purge, "removed_volume": False}
    if state == "running":
        rc, _, e = _run_docker(["stop", spec.container], timeout=60)
        if rc != 0:
            raise QuarryError(
                f"failed to stop container {spec.container}: {e.strip()}",
                exit_code=EXIT_CONNECTION_ERROR,
            )
        result["stopped"] = True
    if purge:
        if state != "absent":
            _run_docker(["rm", "-f", spec.container], timeout=60)
        if volume_exists(spec.volume):
            rc, _, e = _run_docker(["volume", "rm", spec.volume], timeout=30)
            if rc != 0:
                raise QuarryError(
                    f"failed to remove volume {spec.volume}: {e.strip()}",
                    exit_code=EXIT_CONNECTION_ERROR,
                )
            result["removed_volume"] = True
    return result


def engine_status(spec: EngineSpec) -> dict:
    """Read-only container status. Never raises on a missing daemon — reports it."""
    if not docker_available():
        return {"engine": spec.engine, "docker": False, "running": False,
                "state": "unknown", "port": spec.port, "image": None,
                "volume": spec.volume, "volume_exists": False}
    state = container_state(spec.container)
    image = container_image(spec.container) if state != "absent" else None
    return {"engine": spec.engine, "docker": True, "running": state == "running",
            "state": state, "port": spec.port, "image": image,
            "volume": spec.volume, "volume_exists": volume_exists(spec.volume)}


# ---------------------------------------------------------------------------
# connection registration (pure connections.toml read/write — no docker)
# ---------------------------------------------------------------------------

def _logical_of(key: str, fields: dict[str, str]) -> str:
    return fields.get("db") or key


def existing_local_key(data: dict[str, dict[str, str]], logical: str) -> str | None:
    """The key of an already-registered env=local connection for this env-set, if any."""
    for k, f in data.items():
        if _logical_of(k, f) == logical and (f.get("env") or "").lower() == LOCAL_ENV:
            return k
    return None


def _pick_local_key(data: dict[str, dict[str, str]], logical: str) -> str:
    base = f"{logical}_{LOCAL_ENV}"
    if base not in data:
        return base
    i = 2
    while f"{base}{i}" in data:
        i += 1
    return f"{base}{i}"


def stored_local_image(logical: str) -> str | None:
    _, data = core._read_connections_file_parts()
    key = existing_local_key(data, logical)
    return data[key].get("local_image") if key else None


def register_local_connection(
    logical: str, spec: EngineSpec, *, image: str | None = None, group: str | None = None,
) -> tuple[str, bool]:
    """Idempotently ensure an env=local connection for `logical` exists.

    Returns (key, created). If a local connection already exists for this env-set
    it is left untouched (never overwrite user-edited fields) and created=False.
    """
    header, data = core._read_connections_file_parts()
    existing = existing_local_key(data, logical)
    if existing:
        return existing, False
    key = _pick_local_key(data, logical)
    fields: dict[str, str] = {
        "url": spec.url(logical),
        "engine": spec.engine,
        "env": LOCAL_ENV,
        "db": logical,
    }
    if group:
        fields["group"] = group
    if image:
        fields["local_image"] = image
    fields["local_volume"] = spec.volume
    data[key] = fields
    core._write_connections_file(header, data)
    return key, True
