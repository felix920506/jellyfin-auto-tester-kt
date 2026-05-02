"""Docker lifecycle helper for the execution stage.

The public module-level functions are the custom-tool surface used by the
execution agent. ``DockerManager`` is kept injectable so unit tests can cover
the behavior without a local Docker daemon or the Docker SDK installed.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("JF_AUTO_TESTER_ARTIFACTS_ROOT", REPO_ROOT / "artifacts")
).resolve()
IMAGE_RE = re.compile(r"^jellyfin/(?:jellyfin|jellyfin-web):[^\s]+$")
LABEL_KEY = "jf-auto-tester"
LABEL_VALUE = "1"
MAX_RUNNING_CONTAINERS = 2
MAX_CONTAINER_AGE_S = 30 * 60

_CLEANUP_CONTAINER_IDS: set[str] = set()
_SIGNALS_REGISTERED = False
_PREVIOUS_SIGNAL_HANDLERS: dict[int, Any] = {}
_DEFAULT_MANAGER: "DockerManager | None" = None


class DockerManager:
    """Safe wrapper around Docker SDK operations for Stage 2."""

    def __init__(
        self,
        artifacts_root: str | Path | None = None,
        client: Any | None = None,
    ) -> None:
        self.artifacts_root = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT).resolve()
        self._client = client

    def pull(self, image: str, run_id: str | None = None) -> dict[str, Any]:
        """Pull a whitelisted Jellyfin image."""

        _validate_image(image)
        started = time.monotonic()
        self._log(run_id, "pull_start", {"image": image})
        image_obj = self.client.images.pull(image)
        attrs = getattr(image_obj, "attrs", {}) or {}
        result = {
            "image": image,
            "digest": _first(attrs.get("RepoDigests")),
            "size_mb": round(float(attrs.get("Size") or 0) / 1024 / 1024, 2),
            "duration_ms": _elapsed_ms(started),
        }
        self._log(run_id, "pull_done", result)
        return result

    def start(
        self,
        image: str,
        ports: dict[str, Any] | None,
        volumes: list[dict[str, Any]] | dict[str, Any] | None,
        env_vars: dict[str, str] | None,
        run_id: str,
    ) -> dict[str, Any]:
        """Start a Jellyfin container for a run."""

        _validate_image(image)
        if not run_id:
            raise ValueError("run_id is required")

        self._reap_stale_containers()
        name = f"jf-test-{run_id[:8]}"
        self._remove_container_by_name(name, run_id)

        running = self.client.containers.list(
            filters={"label": f"{LABEL_KEY}={LABEL_VALUE}", "status": "running"}
        )
        if len(running) >= MAX_RUNNING_CONTAINERS:
            raise RuntimeError(
                f"refusing to start more than {MAX_RUNNING_CONTAINERS} managed containers"
            )

        container_port, preferred_host_port = _port_mapping(ports)
        docker_volumes = _volume_mapping(volumes)
        for host_path in docker_volumes:
            Path(host_path).mkdir(parents=True, exist_ok=True)

        labels = {
            LABEL_KEY: LABEL_VALUE,
            "jf-auto-tester-run-id": run_id,
            "jf-auto-tester-started-at": datetime.now(timezone.utc).isoformat(),
        }

        host_ports = _candidate_host_ports(preferred_host_port)
        last_error: Exception | None = None
        for host_port in host_ports:
            started = time.monotonic()
            self._log(
                run_id,
                "start_attempt",
                {"image": image, "name": name, "host_port": host_port},
            )
            try:
                container = self.client.containers.run(
                    image,
                    detach=True,
                    name=name,
                    ports={f"{container_port}/tcp": host_port},
                    volumes=docker_volumes,
                    environment=env_vars or {},
                    labels=labels,
                    restart_policy={"Name": "no"},
                )
            except Exception as exc:
                last_error = exc
                if _is_port_allocation_error(exc) and host_port != host_ports[-1]:
                    self._log(
                        run_id,
                        "start_port_retry",
                        {"host_port": host_port, "error": str(exc)},
                    )
                    continue
                self._log(run_id, "start_failed", {"error": str(exc)})
                raise

            container_id = getattr(container, "id", None) or getattr(
                container, "short_id", ""
            )
            status = getattr(container, "status", "created")
            _register_cleanup(container_id, self)
            result = {
                "container_id": container_id,
                "name": name,
                "status": status,
                "host_port": host_port,
                "container_port": container_port,
                "base_url": f"http://localhost:{host_port}",
                "duration_ms": _elapsed_ms(started),
            }
            self._log(run_id, "start_done", result)
            return result

        assert last_error is not None
        raise last_error

    def exec(
        self,
        container_id: str,
        command: str,
        timeout_s: int = 120,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a shell command inside a managed container."""

        started = time.monotonic()
        self._log(
            run_id,
            "exec_start",
            {"container_id": container_id, "command": command, "timeout_s": timeout_s},
        )
        container = self.client.containers.get(container_id)

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            container.exec_run,
            ["/bin/sh", "-lc", command],
            stdout=True,
            stderr=True,
            demux=True,
        )
        try:
            exec_result = future.result(timeout=timeout_s)
        except FutureTimeoutError as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            self._log(run_id, "exec_timeout", {"container_id": container_id})
            raise TimeoutError("timeout") from exc
        else:
            executor.shutdown(wait=True)

        exit_code = getattr(exec_result, "exit_code", None)
        output = getattr(exec_result, "output", b"")
        stdout, stderr = _split_exec_output(output)
        result = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration_ms": _elapsed_ms(started),
        }
        self._log(run_id, "exec_done", {"container_id": container_id, **result})
        return result

    def logs(
        self,
        container_id: str,
        tail: int | str = 500,
        since: int | datetime | None = None,
        run_id: str | None = None,
    ) -> dict[str, str]:
        """Return container logs."""

        container = self.client.containers.get(container_id)
        kwargs: dict[str, Any] = {"tail": tail, "timestamps": True}
        if since is not None:
            kwargs["since"] = since
        raw_logs = container.logs(**kwargs)
        logs = _decode(raw_logs)
        self._log(
            run_id,
            "logs",
            {"container_id": container_id, "tail": tail, "size": len(logs)},
        )
        return {"logs": logs}

    def stop(self, container_id: str, run_id: str | None = None) -> dict[str, str]:
        """Stop and remove a container."""

        status = "removed"
        self._log(run_id, "stop_start", {"container_id": container_id})
        try:
            container = self.client.containers.get(container_id)
            try:
                container.stop(timeout=10)
            except Exception as exc:
                status = "remove_after_stop_error"
                self._log(run_id, "stop_error", {"error": str(exc)})
            container.remove(force=True)
        except Exception as exc:
            status = "not_found_or_remove_failed"
            self._log(run_id, "remove_error", {"error": str(exc)})
        _unregister_cleanup(container_id)
        result = {"status": status}
        self._log(run_id, "stop_done", {"container_id": container_id, **result})
        return result

    def inspect(self, container_id: str, run_id: str | None = None) -> dict[str, Any]:
        """Return Docker inspect output for a container."""

        result = self.client.api.inspect_container(container_id)
        self._log(run_id, "inspect", {"container_id": container_id})
        return result

    @property
    def client(self) -> Any:
        if self._client is None:
            docker = _load_docker()
            self._client = docker.from_env()
        return self._client

    def _remove_container_by_name(self, name: str, run_id: str | None) -> None:
        try:
            container = self.client.containers.get(name)
        except Exception:
            return
        self._log(run_id, "remove_stale_name", {"name": name})
        container.remove(force=True)

    def _reap_stale_containers(self) -> None:
        now = time.time()
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"{LABEL_KEY}={LABEL_VALUE}"},
            )
        except Exception:
            return

        for container in containers:
            attrs = getattr(container, "attrs", {}) or {}
            created = _parse_docker_created(attrs.get("Created"))
            if created is None or now - created < MAX_CONTAINER_AGE_S:
                continue
            container_id = getattr(container, "id", "")
            self._log(None, "reap_stale", {"container_id": container_id})
            try:
                container.remove(force=True)
            except Exception as exc:
                self._log(None, "reap_failed", {"container_id": container_id, "error": str(exc)})

    def _log(self, run_id: str | None, event: str, payload: dict[str, Any]) -> None:
        log_dir = self.artifacts_root / run_id if run_id else self.artifacts_root
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        with (log_dir / "docker_ops.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def pull(image: str, run_id: str | None = None) -> dict[str, Any]:
    return _manager().pull(image=image, run_id=run_id)


def start(
    image: str,
    ports: dict[str, Any] | None = None,
    volumes: list[dict[str, Any]] | dict[str, Any] | None = None,
    env_vars: dict[str, str] | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    return _manager().start(
        image=image,
        ports=ports,
        volumes=volumes,
        env_vars=env_vars,
        run_id=run_id,
    )


def exec(
    container_id: str,
    command: str,
    timeout_s: int = 120,
    run_id: str | None = None,
) -> dict[str, Any]:
    return _manager().exec(
        container_id=container_id,
        command=command,
        timeout_s=timeout_s,
        run_id=run_id,
    )


def logs(
    container_id: str,
    tail: int | str = 500,
    since: int | datetime | None = None,
    run_id: str | None = None,
) -> dict[str, str]:
    return _manager().logs(container_id=container_id, tail=tail, since=since, run_id=run_id)


def stop(container_id: str, run_id: str | None = None) -> dict[str, str]:
    return _manager().stop(container_id=container_id, run_id=run_id)


def inspect(container_id: str, run_id: str | None = None) -> dict[str, Any]:
    return _manager().inspect(container_id=container_id, run_id=run_id)


def _manager() -> DockerManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = DockerManager()
    return _DEFAULT_MANAGER


def _load_docker() -> Any:
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("docker Python SDK is not installed") from exc
    return docker


def _validate_image(image: str) -> None:
    if not IMAGE_RE.fullmatch(image or ""):
        raise ValueError(
            "image must be a tagged jellyfin/jellyfin or jellyfin/jellyfin-web image"
        )


def _port_mapping(ports: dict[str, Any] | None) -> tuple[int, int]:
    if not ports:
        return 8096, 8096
    if "container" in ports or "host" in ports:
        return int(ports.get("container", 8096)), int(ports.get("host", 8096))
    first_key = next(iter(ports))
    return int(str(first_key).split("/")[0]), int(ports[first_key])


def _candidate_host_ports(preferred: int) -> list[int]:
    candidates = [preferred]
    for fallback in (8097, 8098):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _volume_mapping(
    volumes: list[dict[str, Any]] | dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    if not volumes:
        return {}
    if isinstance(volumes, dict):
        return {
            str(host): {
                "bind": str(spec.get("bind") or spec.get("container")),
                "mode": str(spec.get("mode", "rw")),
            }
            for host, spec in volumes.items()
            if isinstance(spec, dict)
        }

    result: dict[str, dict[str, str]] = {}
    for volume in volumes:
        host = Path(str(volume["host"])).expanduser().resolve()
        result[str(host)] = {
            "bind": str(volume["container"]),
            "mode": str(volume.get("mode", "rw")),
        }
    return result


def _split_exec_output(output: Any) -> tuple[str, str]:
    if isinstance(output, tuple):
        stdout, stderr = output
        return _decode(stdout), _decode(stderr)
    return _decode(output), ""


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _is_port_allocation_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "port is already allocated" in message or "bind" in message


def _parse_docker_created(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    if "." in normalized:
        head, tail = normalized.split(".", 1)
        timezone_part = ""
        if "+" in tail:
            fraction, timezone_part = tail.split("+", 1)
            timezone_part = "+" + timezone_part
        elif "-" in tail:
            fraction, timezone_part = tail.split("-", 1)
            timezone_part = "-" + timezone_part
        else:
            fraction = tail
        normalized = f"{head}.{fraction[:6]}{timezone_part}"
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _register_cleanup(container_id: str, manager: DockerManager) -> None:
    if not container_id:
        return
    _CLEANUP_CONTAINER_IDS.add(container_id)
    _register_exit_hooks(manager)


def _unregister_cleanup(container_id: str) -> None:
    _CLEANUP_CONTAINER_IDS.discard(container_id)


def _register_exit_hooks(manager: DockerManager) -> None:
    global _SIGNALS_REGISTERED
    if not _SIGNALS_REGISTERED:
        atexit.register(_cleanup_registered_containers, manager)
        for signum in (signal.SIGTERM, signal.SIGINT):
            _PREVIOUS_SIGNAL_HANDLERS[signum] = signal.getsignal(signum)
            signal.signal(signum, _signal_cleanup_handler(manager))
        _SIGNALS_REGISTERED = True


def _signal_cleanup_handler(manager: DockerManager) -> Any:
    def handler(signum: int, frame: Any) -> None:
        _cleanup_registered_containers(manager)
        previous = _PREVIOUS_SIGNAL_HANDLERS.get(signum)
        if callable(previous):
            previous(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        else:
            raise SystemExit(128 + signum)

    return handler


def _cleanup_registered_containers(manager: DockerManager) -> None:
    for container_id in list(_CLEANUP_CONTAINER_IDS):
        try:
            manager.stop(container_id)
        except Exception:
            _CLEANUP_CONTAINER_IDS.discard(container_id)


try:
    DockerManager()._reap_stale_containers()
except Exception:
    pass
