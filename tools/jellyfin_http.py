"""Raw Jellyfin HTTP transport for the execution stage."""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("JF_AUTO_TESTER_ARTIFACTS_ROOT", REPO_ROOT / "artifacts")
).resolve()
DEFAULT_BASE_URL = "http://localhost:8096"
CLIENT_HEADER = (
    'MediaBrowser Client="Jellyfin Auto Tester", '
    'Device="Stage2", DeviceId="jellyfin-auto-tester-stage2", Version="1.0"'
)

_DEFAULT_HTTP: "JellyfinHTTP | None" = None


class JellyfinHTTP:
    """Raw Jellyfin HTTP transport with token persistence and request logging."""

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
        params: Any = None,
        headers: Any = None,
        auth: str = "auto",
        token: str | None = None,
        body_json: Any = None,
        body_text: str | None = None,
        body_base64: str | None = None,
        timeout_s: float | int | None = None,
        follow_redirects: bool = False,
        allow_absolute_url: bool = False,
    ) -> dict[str, Any]:
        """Make a raw HTTP request and return a serializable response record."""

        return self._request_with_retries(
            method=method,
            path=path,
            params=params,
            headers=headers,
            auth=auth,
            token=token,
            body_json=body_json,
            body_text=body_text,
            body_base64=body_base64,
            timeout_s=timeout_s,
            follow_redirects=follow_redirects,
            allow_absolute_url=allow_absolute_url,
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
                health = self._request_once("GET", "/health", auth="none")
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
                        auth="none",
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
            request_kwargs = {"body_json": body} if body is not None else {}
            response = self.request("POST", path, auth="none", **request_kwargs)
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
            auth="none",
            body_json={"Username": username, "Pw": password},
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
        params: Any,
        headers: Any,
        auth: str,
        token: str | None,
        body_json: Any,
        body_text: str | None,
        body_base64: str | None,
        timeout_s: float | int | None,
        follow_redirects: bool,
        allow_absolute_url: bool,
        max_attempts: int,
        backoff_s: int,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self._request_once(
                    method,
                    path,
                    params=params,
                    headers=headers,
                    auth=auth,
                    token=token,
                    body_json=body_json,
                    body_text=body_text,
                    body_base64=body_base64,
                    timeout_s=timeout_s,
                    follow_redirects=follow_redirects,
                    allow_absolute_url=allow_absolute_url,
                    attempt=attempt,
                )
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
        *,
        params: Any = None,
        headers: Any = None,
        auth: str = "auto",
        token: str | None = None,
        body_json: Any = None,
        body_text: str | None = None,
        body_base64: str | None = None,
        timeout_s: float | int | None = None,
        follow_redirects: bool = False,
        allow_absolute_url: bool = False,
        attempt: int = 1,
    ) -> dict[str, Any]:
        started = time.monotonic()
        request_headers = _apply_auth_headers(
            _normalize_headers(headers),
            auth=auth,
            stored_token=self.token,
            token=token,
        )
        body_kwargs, body_mode, body_for_log = _body_kwargs(
            body_json=body_json,
            body_text=body_text,
            body_base64=body_base64,
        )

        kwargs: dict[str, Any] = {
            "headers": request_headers,
            "follow_redirects": bool(follow_redirects),
            **body_kwargs,
        }
        if params is not None:
            kwargs["params"] = params
        if timeout_s is not None:
            kwargs["timeout"] = float(timeout_s)

        url = self._url(path, allow_absolute_url=allow_absolute_url)
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
                "request_params": params,
                "request_headers": _redact_headers(request_headers),
                "request_auth": auth,
                "request_body_mode": body_mode,
                "request_body": body_for_log,
                "follow_redirects": bool(follow_redirects),
                "status_code": result["status_code"],
                "duration_ms": result["duration_ms"],
                "response_headers": result["headers"],
                "response_body": result["body"],
            }
        )
        return result

    def _url(self, path: str, *, allow_absolute_url: bool) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            if not allow_absolute_url:
                raise ValueError("absolute URLs require allow_absolute_url=true")
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
    return _http().configure(base_url=base_url, run_id=run_id)


def request(
    method: str,
    path: str,
    params: Any = None,
    headers: Any = None,
    auth: str = "auto",
    token: str | None = None,
    body_json: Any = None,
    body_text: str | None = None,
    body_base64: str | None = None,
    timeout_s: float | int | None = None,
    follow_redirects: bool = False,
    allow_absolute_url: bool = False,
) -> dict[str, Any]:
    return _http().request(
        method=method,
        path=path,
        params=params,
        headers=headers,
        auth=auth,
        token=token,
        body_json=body_json,
        body_text=body_text,
        body_base64=body_base64,
        timeout_s=timeout_s,
        follow_redirects=follow_redirects,
        allow_absolute_url=allow_absolute_url,
    )


def wait_healthy(timeout_s: int = 60) -> dict[str, Any]:
    return _http().wait_healthy(timeout_s=timeout_s)


def authenticate(
    username: str = "admin",
    password: str = "admin",
) -> dict[str, Any]:
    return _http().authenticate(username=username, password=password)


def complete_startup_wizard(
    admin_user: str = "admin",
    admin_password: str = "admin",
) -> dict[str, Any]:
    return _http().complete_startup_wizard(
        admin_user=admin_user,
        admin_password=admin_password,
    )


def _http() -> JellyfinHTTP:
    global _DEFAULT_HTTP
    if _DEFAULT_HTTP is None:
        _DEFAULT_HTTP = JellyfinHTTP()
    return _DEFAULT_HTTP


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


def _normalize_headers(headers: Any) -> dict[str, str] | list[tuple[str, str]]:
    if headers is None:
        return {}
    if isinstance(headers, dict):
        return {str(key): str(value) for key, value in headers.items()}
    if isinstance(headers, list):
        normalized: list[tuple[str, str]] = []
        for item in headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("headers list entries must be [name, value] pairs")
            normalized.append((str(item[0]), str(item[1])))
        return normalized
    raise ValueError("headers must be an object or a list of [name, value] pairs")


def _apply_auth_headers(
    headers: dict[str, str] | list[tuple[str, str]],
    *,
    auth: str,
    stored_token: str | None,
    token: str | None,
) -> dict[str, str] | list[tuple[str, str]]:
    if auth not in {"auto", "none", "token"}:
        raise ValueError("auth must be one of: auto, none, token")
    if auth == "none":
        return headers
    auth_token = stored_token if auth == "auto" else token
    if not auth_token:
        if auth == "token" and not _has_header(headers, "X-Emby-Token"):
            raise ValueError("auth=token requires token")
        return headers
    if _has_header(headers, "X-Emby-Token"):
        return headers
    return _with_header(headers, "X-Emby-Token", auth_token)


def _has_header(headers: dict[str, str] | list[tuple[str, str]], name: str) -> bool:
    target = name.lower()
    if isinstance(headers, dict):
        return any(str(key).lower() == target for key in headers)
    return any(key.lower() == target for key, _value in headers)


def _with_header(
    headers: dict[str, str] | list[tuple[str, str]],
    name: str,
    value: str,
) -> dict[str, str] | list[tuple[str, str]]:
    if isinstance(headers, dict):
        updated = dict(headers)
        updated[name] = value
        return updated
    return [*headers, (name, value)]


def _body_kwargs(
    *,
    body_json: Any,
    body_text: str | None,
    body_base64: str | None,
) -> tuple[dict[str, Any], str | None, Any]:
    modes = [
        name
        for name, value in (
            ("body_json", body_json),
            ("body_text", body_text),
            ("body_base64", body_base64),
        )
        if value is not None
    ]
    if len(modes) > 1:
        raise ValueError("use exactly one of body_json, body_text, or body_base64")
    if body_json is not None:
        return {"json": body_json}, "body_json", body_json
    if body_text is not None:
        return {"content": body_text}, "body_text", body_text
    if body_base64 is not None:
        try:
            return (
                {"content": base64.b64decode(str(body_base64), validate=True)},
                "body_base64",
                body_base64,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError("body_base64 must be valid base64") from exc
    return {}, None, None


def _redact_headers(headers: dict[str, str] | list[tuple[str, str]]) -> Any:
    sensitive = {"authorization", "x-emby-token"}

    def redact(name: str, value: str) -> str:
        return "[redacted]" if name.lower() in sensitive else value

    if isinstance(headers, dict):
        return {key: redact(str(key), str(value)) for key, value in headers.items()}
    return [(key, redact(key, value)) for key, value in headers]
