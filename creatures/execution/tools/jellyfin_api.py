"""Typed Jellyfin HTTP helper for the execution stage."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("JF_AUTO_TESTER_ARTIFACTS_ROOT", REPO_ROOT / "artifacts")
).resolve()
DEFAULT_BASE_URL = "http://localhost:8096"
CLIENT_HEADER = (
    'MediaBrowser Client="Jellyfin Auto Tester", '
    'Device="Stage2", DeviceId="jellyfin-auto-tester-stage2", Version="1.0"'
)

_DEFAULT_API: "JellyfinAPI | None" = None


class JellyfinAPI:
    """Small Jellyfin REST client with token persistence and request logging."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        run_id: str | None = None,
        artifacts_root: str | Path | None = None,
        client: Any | None = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.artifacts_root = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT).resolve()
        self._client = client
        self._sleep = sleep_fn
        self.token: str | None = None

    def configure(
        self,
        base_url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, str | None]:
        if base_url is not None:
            self.base_url = base_url.rstrip("/")
        if run_id is not None:
            self.run_id = run_id
        return {"base_url": self.base_url, "run_id": self.run_id}

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request and return a serializable response record."""

        return self._request_with_retries(
            method=method,
            path=path,
            body=body,
            headers=headers,
            max_attempts=5,
            backoff_s=2,
        )

    def wait_healthy(self, timeout_s: int = 60) -> dict[str, Any]:
        """Poll Jellyfin until either /health or /System/Info/Public responds."""

        started = time.monotonic()
        last_error: str | None = None
        endpoint_used: str | None = None
        while time.monotonic() - started < timeout_s:
            try:
                health = self._request_once("GET", "/health", None, None)
                if health["status_code"] == 200 and "Healthy" in (health["body"] or ""):
                    endpoint_used = "/health"
                    return {
                        "healthy": True,
                        "elapsed_s": round(time.monotonic() - started, 2),
                        "endpoint_used": endpoint_used,
                    }
                if health["status_code"] in {404, 405}:
                    fallback = self._request_once(
                        "GET",
                        "/System/Info/Public",
                        None,
                        None,
                    )
                    if fallback["status_code"] == 200:
                        endpoint_used = "/System/Info/Public"
                        return {
                            "healthy": True,
                            "elapsed_s": round(time.monotonic() - started, 2),
                            "endpoint_used": endpoint_used,
                        }
                    last_error = f"fallback status {fallback['status_code']}"
                else:
                    last_error = f"health status {health['status_code']}"
            except Exception as exc:
                last_error = str(exc)

            self._sleep(1)

        return {
            "healthy": False,
            "elapsed_s": round(time.monotonic() - started, 2),
            "endpoint_used": endpoint_used,
            "error": last_error,
        }

    def complete_startup_wizard(
        self,
        admin_user: str = "admin",
        admin_password: str = "admin",
    ) -> dict[str, Any]:
        """Provision the first-run wizard, treating 403/404 as already done."""

        started = time.monotonic()
        steps = [
            (
                "/Startup/Configuration",
                {
                    "UICulture": "en-US",
                    "MetadataCountryCode": "US",
                    "PreferredMetadataLanguage": "en",
                },
            ),
            ("/Startup/User", {"Name": admin_user, "Password": admin_password}),
            (
                "/Startup/RemoteAccess",
                {
                    "EnableRemoteAccess": True,
                    "EnableAutomaticPortMapping": False,
                },
            ),
            ("/Startup/Complete", None),
        ]

        responses = []
        for path, body in steps:
            response = self.request("POST", path, body=body)
            responses.append({"path": path, "status_code": response["status_code"]})
            if response["status_code"] in {403, 404}:
                return {
                    "provisioned": False,
                    "already_provisioned": True,
                    "elapsed_s": round(time.monotonic() - started, 2),
                    "responses": responses,
                }

        success = all(200 <= item["status_code"] < 300 for item in responses)
        return {
            "provisioned": success,
            "already_provisioned": False,
            "elapsed_s": round(time.monotonic() - started, 2),
            "responses": responses,
        }

    def authenticate(
        self,
        username: str = "admin",
        password: str = "admin",
    ) -> dict[str, Any]:
        """Authenticate as a Jellyfin user and store the access token."""

        response = self.request(
            "POST",
            "/Users/AuthenticateByName",
            body={"Username": username, "Pw": password},
            headers={"Authorization": CLIENT_HEADER},
        )
        token = None
        user_id = None
        if 200 <= response["status_code"] < 300:
            try:
                payload = json.loads(response["body"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            token = payload.get("AccessToken")
            user = payload.get("User")
            if isinstance(user, dict):
                user_id = user.get("Id")

        success = bool(token)
        if success:
            self.token = str(token)
        return {
            "token": token,
            "user_id": user_id,
            "success": success,
            "status_code": response["status_code"],
        }

    @property
    def client(self) -> Any:
        if self._client is None:
            httpx = _load_httpx()
            timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
            self._client = httpx.Client(timeout=timeout)
        return self._client

    def _request_with_retries(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str] | None,
        max_attempts: int,
        backoff_s: int,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self._request_once(method, path, body, headers, attempt=attempt)
            except Exception as exc:
                last_error = exc
                self._log(
                    {
                        "method": method.upper(),
                        "path": path,
                        "attempt": attempt,
                        "error": str(exc),
                    }
                )
                if not _is_connection_refused(exc) or attempt == max_attempts:
                    raise
                self._sleep(backoff_s)
        assert last_error is not None
        raise last_error

    def _request_once(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str] | None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        started = time.monotonic()
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if self.token and "X-Emby-Token" not in request_headers:
            request_headers["X-Emby-Token"] = self.token

        kwargs: dict[str, Any] = {"headers": request_headers}
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        elif body is not None:
            kwargs["content"] = str(body)

        url = self._url(path)
        response = self.client.request(method.upper(), url, **kwargs)
        result = {
            "status_code": int(getattr(response, "status_code")),
            "body": getattr(response, "text", ""),
            "headers": dict(getattr(response, "headers", {}) or {}),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        self._log(
            {
                "method": method.upper(),
                "path": path,
                "url": url,
                "attempt": attempt,
                "request_body": body,
                "status_code": result["status_code"],
                "duration_ms": result["duration_ms"],
                "response_headers": result["headers"],
                "response_body": result["body"],
            }
        )
        return result

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _log(self, record: dict[str, Any]) -> None:
        log_dir = self.artifacts_root / self.run_id if self.run_id else self.artifacts_root
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        with (log_dir / "http_log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def configure(
    base_url: str | None = None,
    run_id: str | None = None,
) -> dict[str, str | None]:
    return _api().configure(base_url=base_url, run_id=run_id)


def request(
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _api().request(method=method, path=path, body=body, headers=headers)


def wait_healthy(timeout_s: int = 60) -> dict[str, Any]:
    return _api().wait_healthy(timeout_s=timeout_s)


def authenticate(
    username: str = "admin",
    password: str = "admin",
) -> dict[str, Any]:
    return _api().authenticate(username=username, password=password)


def complete_startup_wizard(
    admin_user: str = "admin",
    admin_password: str = "admin",
) -> dict[str, Any]:
    return _api().complete_startup_wizard(
        admin_user=admin_user,
        admin_password=admin_password,
    )


def _api() -> JellyfinAPI:
    global _DEFAULT_API
    if _DEFAULT_API is None:
        _DEFAULT_API = JellyfinAPI()
    return _DEFAULT_API


def _load_httpx() -> Any:
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("httpx is not installed") from exc
    return httpx


def _is_connection_refused(exc: Exception) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    text = str(exc).lower()
    return "connection refused" in text or "connecterror" in text
