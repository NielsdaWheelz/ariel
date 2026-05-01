from __future__ import annotations

import argparse
from dataclasses import dataclass
from ipaddress import ip_address
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Mapping
from urllib.parse import urlparse


_DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/ariel"


@dataclass(frozen=True)
class LocalPostgresRuntime:
    database_url: str
    host: str
    host_port: int
    user: str
    password: str
    database: str
    container_name: str
    image: str
    volume_name: str


def parse_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, raw_value = line.split("=", maxsplit=1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_local_env(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    merged.update(parse_dotenv_file(project_root / ".env"))
    merged.update(parse_dotenv_file(project_root / ".env.local"))
    merged.update(dict(environ or os.environ))
    return merged


def resolve_local_postgres_runtime(env: Mapping[str, str]) -> LocalPostgresRuntime:
    database_url = env.get("ARIEL_DATABASE_URL", _DEFAULT_DATABASE_URL).strip()
    parsed = urlparse(database_url)
    if not parsed.scheme.startswith("postgresql"):
        msg = "ARIEL_DATABASE_URL must use a PostgreSQL scheme"
        raise ValueError(msg)

    host = (parsed.hostname or "localhost").strip().lower()
    if not _is_loopback_host(host):
        msg = (
            "ARIEL_DATABASE_URL host must be loopback (localhost/127.0.0.1/::1) "
            "for local docker-managed Postgres"
        )
        raise ValueError(msg)

    host_port = parsed.port or 5432
    user = parsed.username or "ariel"
    password = parsed.password or "change-me-dev"
    database = parsed.path.lstrip("/") or "ariel"

    container_name = (
        env.get("ARIEL_DB_CONTAINER_NAME", "ariel-postgres").strip() or "ariel-postgres"
    )
    image = env.get("ARIEL_DB_DOCKER_IMAGE", "pgvector/pgvector:pg16").strip()
    if not image:
        image = "pgvector/pgvector:pg16"
    volume_name = env.get("ARIEL_DB_VOLUME_NAME", f"{container_name}-data").strip()
    if not volume_name:
        volume_name = f"{container_name}-data"

    return LocalPostgresRuntime(
        database_url=database_url,
        host=host,
        host_port=host_port,
        user=user,
        password=password,
        database=database,
        container_name=container_name,
        image=image,
        volume_name=volume_name,
    )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _run(
    cmd: list[str], *, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def _container_exists(container_name: str) -> bool:
    result = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=^{container_name}$",
            "--format",
            "{{.Names}}",
        ],
        capture=True,
    )
    return any(line.strip() == container_name for line in result.stdout.splitlines())


def _container_host_port(container_name: str) -> int | None:
    """Return the published host port for container port 5432, or None."""
    result = _run(
        [
            "docker",
            "inspect",
            "--format",
            '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}',
            container_name,
        ],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _container_running(container_name: str) -> bool:
    result = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"name=^{container_name}$",
            "--format",
            "{{.Names}}",
        ],
        capture=True,
    )
    return any(line.strip() == container_name for line in result.stdout.splitlines())


def _wait_until_ready(runtime: LocalPostgresRuntime, *, timeout_seconds: int = 45) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = _run(
            [
                "docker",
                "exec",
                runtime.container_name,
                "pg_isready",
                "-U",
                runtime.user,
                "-d",
                runtime.database,
            ],
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    msg = (
        f"postgres container '{runtime.container_name}' did not become ready within "
        f"{timeout_seconds}s"
    )
    raise RuntimeError(msg)


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) != 0


def cmd_up(runtime: LocalPostgresRuntime) -> int:
    if _container_running(runtime.container_name):
        print(f"postgres container '{runtime.container_name}' is already running")
        return 0

    if not _port_available("127.0.0.1", runtime.host_port):
        print(
            f"error: port {runtime.host_port} is already in use — "
            "update ARIEL_DATABASE_URL in .env.local to use a free port"
        )
        return 1

    if _container_exists(runtime.container_name):
        existing_port = _container_host_port(runtime.container_name)
        if existing_port is not None and existing_port != runtime.host_port:
            print(
                f"existing container '{runtime.container_name}' is bound to port "
                f"{existing_port}, but config expects {runtime.host_port} — recreating"
            )
            _run(["docker", "rm", runtime.container_name])
        else:
            _run(["docker", "start", runtime.container_name])
            _wait_until_ready(runtime)
            print(f"postgres container '{runtime.container_name}' started")
            return 0

    _run(
        [
            "docker",
            "run",
            "--name",
            runtime.container_name,
            "-d",
            "-e",
            f"POSTGRES_USER={runtime.user}",
            "-e",
            f"POSTGRES_PASSWORD={runtime.password}",
            "-e",
            f"POSTGRES_DB={runtime.database}",
            "-p",
            f"127.0.0.1:{runtime.host_port}:5432",
            "-v",
            f"{runtime.volume_name}:/var/lib/postgresql/data",
            "--health-cmd",
            f"pg_isready -U {runtime.user} -d {runtime.database}",
            "--health-interval",
            "5s",
            "--health-timeout",
            "5s",
            "--health-retries",
            "12",
            runtime.image,
        ]
    )
    _wait_until_ready(runtime)
    print(
        "postgres container created and running: "
        f"name={runtime.container_name} image={runtime.image} "
        f"port=127.0.0.1:{runtime.host_port} db={runtime.database} user={runtime.user}"
    )
    return 0


def cmd_stop(runtime: LocalPostgresRuntime) -> int:
    if not _container_exists(runtime.container_name):
        print(f"postgres container '{runtime.container_name}' does not exist")
        return 0
    if not _container_running(runtime.container_name):
        print(f"postgres container '{runtime.container_name}' is already stopped")
        return 0
    _run(["docker", "stop", runtime.container_name])
    print(f"postgres container '{runtime.container_name}' stopped")
    return 0


def cmd_down(runtime: LocalPostgresRuntime) -> int:
    if not _container_exists(runtime.container_name):
        print(f"postgres container '{runtime.container_name}' does not exist")
        return 0
    _run(["docker", "rm", "-f", runtime.container_name])
    print(f"postgres container '{runtime.container_name}' removed (volume preserved)")
    return 0


def cmd_destroy(runtime: LocalPostgresRuntime) -> int:
    cmd_down(runtime)
    _run(["docker", "volume", "rm", runtime.volume_name], check=False)
    print(f"postgres volume '{runtime.volume_name}' removed (if it existed)")
    return 0


def cmd_status(runtime: LocalPostgresRuntime) -> int:
    print(f"database_url={runtime.database_url}")
    print(f"container_name={runtime.container_name}")
    print(f"image={runtime.image}")
    print(f"volume={runtime.volume_name}")
    _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=^{runtime.container_name}$",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        ]
    )
    return 0


def cmd_logs(runtime: LocalPostgresRuntime, *, follow: bool) -> int:
    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    cmd.extend(["--tail", "200", runtime.container_name])
    _run(cmd, check=False)
    return 0


def cmd_print_config(runtime: LocalPostgresRuntime) -> int:
    print(f"database_url={runtime.database_url}")
    print(f"db_user={runtime.user}")
    print(f"db_name={runtime.database}")
    print(f"db_host={runtime.host}")
    print(f"db_host_port={runtime.host_port}")
    print(f"container_name={runtime.container_name}")
    print(f"volume_name={runtime.volume_name}")
    print(f"image={runtime.image}")
    return 0


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage local Docker Postgres for Ariel development."
    )
    parser.add_argument(
        "action",
        choices=["up", "stop", "down", "destroy", "status", "logs", "print-config"],
    )
    parser.add_argument("--project-root", type=Path, default=_default_project_root())
    parser.add_argument(
        "--follow", action="store_true", help="Follow logs stream (logs action only)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = load_local_env(args.project_root)
    runtime = resolve_local_postgres_runtime(env)

    try:
        if args.action == "up":
            return cmd_up(runtime)
        if args.action == "stop":
            return cmd_stop(runtime)
        if args.action == "down":
            return cmd_down(runtime)
        if args.action == "destroy":
            return cmd_destroy(runtime)
        if args.action == "status":
            return cmd_status(runtime)
        if args.action == "logs":
            return cmd_logs(runtime, follow=args.follow)
        if args.action == "print-config":
            return cmd_print_config(runtime)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {' '.join(exc.cmd)}")
        return exc.returncode
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 1
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
