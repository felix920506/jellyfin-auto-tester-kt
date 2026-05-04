"""Playwright browser driver for Stage 2 Jellyfin Web reproductions."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin

from tools.screenshot import (
    DEFAULT_ARTIFACTS_ROOT,
    _load_sync_playwright,
    browser_locale,
    browser_should_run_headless,
)


DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
DEFAULT_TIMEOUT_S = 30
CONSOLE_TYPES = {"warning", "error"}

MEDIA_STATE_SCRIPT = """() => Array.from(document.querySelectorAll('audio, video')).map((el) => ({
  tag: el.tagName.toLowerCase(),
  id: el.id || null,
  currentTime: el.currentTime,
  duration: Number.isFinite(el.duration) ? el.duration : null,
  paused: el.paused,
  ended: el.ended,
  error: el.error ? {
    code: el.error.code,
    message: el.error.message || null
  } : null,
  readyState: el.readyState,
  src: el.currentSrc || el.src || null
}))"""

DOM_SUMMARY_SCRIPT = """() => {
  const body = document.body;
  const text = body ? body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 2000) : '';
  const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
    .slice(0, 20)
    .map((el) => (el.innerText || el.getAttribute('aria-label') || el.id || '').trim())
    .filter(Boolean);
  const inputs = Array.from(document.querySelectorAll('input,textarea,select'))
    .slice(0, 20)
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      name: el.getAttribute('name'),
      label: el.getAttribute('aria-label'),
      placeholder: el.getAttribute('placeholder')
    }));
  return {
    title: document.title,
    url: location.href,
    text,
    buttons,
    inputs,
    media_count: document.querySelectorAll('audio, video').length
  };
}"""

MEDIA_WAIT_SCRIPT = """(expected) => {
  const elements = Array.from(document.querySelectorAll('audio, video'));
  return elements.some((el) => {
    if (expected === 'playing') return !el.paused && !el.ended && !el.error;
    if (expected === 'paused') return el.paused && !el.ended && !el.error;
    if (expected === 'ended') return el.ended;
    if (expected === 'errored') return Boolean(el.error);
    return false;
  });
}"""


class BrowserDriver:
    """Run the Stage 2 browser action DSL with one persistent browser session."""

    def __init__(
        self,
        artifacts_root: str | Path | None = None,
        base_url: str | None = None,
        run_id: str | None = None,
        playwright_factory: Any | None = None,
        locale: str | None = None,
    ) -> None:
        self.artifacts_root = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT).resolve()
        self.base_url = base_url or "http://localhost:8096"
        self.run_id = run_id
        self._playwright_factory = playwright_factory
        self.locale = browser_locale(locale)
        self._playwright_cm: Any | None = None
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._viewport: dict[str, int] | None = None
        self._locale: str | None = None
        self._console_messages: list[dict[str, Any]] = []
        self._failed_network: list[dict[str, Any]] = []

    def configure(
        self,
        base_url: str | None = None,
        run_id: str | None = None,
        locale: str | None = None,
    ) -> None:
        if base_url:
            self.base_url = base_url
        if run_id:
            self.run_id = run_id
        if locale is not None:
            self.locale = browser_locale(locale)

    def run(
        self,
        browser_input: Mapping[str, Any],
        run_id: str | None = None,
        step_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Execute a browser step and return serializable browser evidence."""

        if run_id:
            self.run_id = run_id
        if not self.run_id:
            raise ValueError("run_id is required for browser artifacts")

        timeout_s = float(browser_input.get("timeout_s") or DEFAULT_TIMEOUT_S)
        viewport = _viewport(browser_input.get("viewport"))
        locale = browser_locale(browser_input.get("locale") or self.locale)
        page = self._ensure_page(viewport, locale)
        console_start = len(self._console_messages)
        network_start = len(self._failed_network)
        actions: list[dict[str, Any]] = []
        screenshot_paths: list[str] = []
        error: str | None = None

        for index, action in enumerate(browser_input.get("actions") or [], start=1):
            if not isinstance(action, Mapping):
                action = {"type": "invalid", "value": action}
            record = self._run_action(
                page=page,
                browser_input=browser_input,
                action=action,
                action_index=index,
                timeout_s=timeout_s,
            )
            actions.append(record)
            if record.get("screenshot_path"):
                screenshot_paths.append(str(record["screenshot_path"]))
            if record.get("status") == "fail":
                error = str(record.get("error") or "browser action failed")
                break

        evidence = self._collect_evidence(
            page=page,
            browser_input=browser_input,
            step_id=step_id,
            actions=actions,
            screenshot_paths=screenshot_paths,
            console_start=console_start,
            network_start=network_start,
            error=error,
        )
        return evidence

    def close(self) -> None:
        """Close the persistent browser session."""

        for target in (self._context, self._browser):
            if target is None:
                continue
            try:
                target.close()
            except Exception:
                pass
        if self._playwright_cm is not None:
            try:
                self._playwright_cm.__exit__(None, None, None)
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._page = None
        self._playwright = None
        self._playwright_cm = None
        self._locale = None

    def _ensure_page(self, viewport: dict[str, int], locale: str) -> Any:
        if (
            self._page is not None
            and self._viewport == viewport
            and self._locale == locale
        ):
            return self._page
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._locale = None
            self._page = None

        if self._playwright is None:
            factory = self._playwright_factory or _load_sync_playwright()
            self._playwright_cm = factory()
            self._playwright = self._playwright_cm.__enter__()
        if self._browser is None:
            self._browser = self._playwright.chromium.launch(
                headless=browser_should_run_headless()
            )

        self._context = self._browser.new_context(viewport=viewport, locale=locale)
        self._page = self._context.new_page()
        self._viewport = viewport
        self._locale = locale
        self._register_page_handlers(self._page)
        return self._page

    def _register_page_handlers(self, page: Any) -> None:
        _safe_on(page, "console", self._handle_console)
        _safe_on(page, "response", self._handle_response)
        _safe_on(page, "requestfailed", self._handle_request_failed)

    def _run_action(
        self,
        page: Any,
        browser_input: Mapping[str, Any],
        action: Mapping[str, Any],
        action_index: int,
        timeout_s: float,
    ) -> dict[str, Any]:
        started = time.monotonic()
        action_type = str(action.get("type") or "")
        timeout_ms = _timeout_ms(action, timeout_s)
        record: dict[str, Any] = {
            "type": action_type,
            "status": "pass",
            "timestamp": _timestamp(),
        }
        for key in ("selector", "label", "state", "key", "path", "url"):
            if action.get(key) is not None:
                record[key] = action.get(key)
        if "value" in action:
            record["value_metadata"] = _safe_value_metadata(action.get("value"))
        if "text" in action:
            record["text_metadata"] = _safe_value_metadata(action.get("text"))

        try:
            if action_type == "goto":
                self._action_goto(page, browser_input, action, timeout_ms)
            elif action_type == "refresh":
                self._action_refresh(page, action, timeout_ms)
            elif action_type == "click":
                page.locator(_required_selector(action)).click(timeout=timeout_ms)
                self._wait_for_app_idle(page, timeout_ms)
            elif action_type == "fill":
                page.locator(_required_selector(action)).fill(
                    str(action.get("value", "")),
                    timeout=timeout_ms,
                )
            elif action_type == "press":
                self._action_press(page, action, timeout_ms)
            elif action_type == "select_option":
                page.locator(_required_selector(action)).select_option(
                    action.get("value"),
                    timeout=timeout_ms,
                )
            elif action_type == "check":
                page.locator(_required_selector(action)).check(timeout=timeout_ms)
            elif action_type == "uncheck":
                page.locator(_required_selector(action)).uncheck(timeout=timeout_ms)
            elif action_type == "wait_for":
                page.locator(_required_selector(action)).wait_for(
                    state=str(action.get("state") or "visible"),
                    timeout=timeout_ms,
                )
            elif action_type == "wait_for_text":
                self._action_wait_for_text(page, action, timeout_ms)
            elif action_type == "wait_for_url":
                self._action_wait_for_url(page, action, timeout_ms)
            elif action_type == "wait_for_media":
                self._action_wait_for_media(page, action, timeout_ms)
            elif action_type == "evaluate":
                result = self._action_evaluate(page, action)
                record["result_metadata"] = _safe_value_metadata(result)
            elif action_type == "screenshot":
                record["screenshot_path"] = self._action_screenshot(
                    page=page,
                    browser_input=browser_input,
                    action=action,
                    action_index=action_index,
                )
            else:
                raise ValueError(f"unsupported browser action: {action_type}")
        except Exception as exc:
            record["status"] = "fail"
            record["error"] = str(exc)
        record["duration_ms"] = _elapsed_ms(started)
        return record

    def _action_goto(
        self,
        page: Any,
        browser_input: Mapping[str, Any],
        action: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        url = self._resolve_url(action.get("url") or action.get("path") or browser_input.get("url") or browser_input.get("path") or "/web")
        page.goto(
            url,
            wait_until=str(action.get("wait_until") or "networkidle"),
            timeout=timeout_ms,
        )
        self._maybe_authenticate(page, browser_input, timeout_ms)
        self._wait_for_app_idle(page, timeout_ms)

    def _action_refresh(
        self,
        page: Any,
        action: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        page.reload(
            wait_until=str(action.get("wait_until") or "networkidle"),
            timeout=timeout_ms,
        )
        self._wait_for_app_idle(page, timeout_ms)

    def _action_press(
        self,
        page: Any,
        action: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        key = str(action.get("key") or action.get("value") or "")
        if not key:
            raise ValueError("press action requires key")
        if action.get("selector"):
            page.locator(str(action["selector"])).press(key, timeout=timeout_ms)
        else:
            page.keyboard.press(key)

    def _action_wait_for_text(
        self,
        page: Any,
        action: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        text = str(action.get("text") or action.get("value") or "")
        if not text:
            raise ValueError("wait_for_text action requires text")
        if action.get("selector"):
            locator = page.locator(str(action["selector"]))
            locator.wait_for(timeout=timeout_ms)
            content = locator.text_content(timeout=timeout_ms) or ""
            if text not in content:
                raise TimeoutError(f"text not found in selector: {text}")
            return
        page.wait_for_function(
            "expected => document.body && document.body.innerText.includes(expected)",
            text,
            timeout=timeout_ms,
        )

    def _action_wait_for_url(
        self,
        page: Any,
        action: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        if action.get("pattern"):
            target = re.compile(str(action["pattern"]))
        else:
            target = self._resolve_url(action.get("url") or action.get("path") or "")
        page.wait_for_url(target, timeout=timeout_ms)

    def _action_wait_for_media(
        self,
        page: Any,
        action: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        state = str(action.get("state") or action.get("value") or "playing")
        page.wait_for_function(MEDIA_WAIT_SCRIPT, state, timeout=timeout_ms)

    def _action_evaluate(self, page: Any, action: Mapping[str, Any]) -> Any:
        script = action.get("script") or action.get("expression")
        if not script:
            raise ValueError("evaluate action requires script or expression")
        if "args" in action:
            return page.evaluate(str(script), action.get("args"))
        return page.evaluate(str(script))

    def _action_screenshot(
        self,
        page: Any,
        browser_input: Mapping[str, Any],
        action: Mapping[str, Any],
        action_index: int,
    ) -> str:
        label = str(
            action.get("label")
            or browser_input.get("label")
            or f"browser_action_{action_index}"
        )
        path = self._screenshot_path(self.run_id or "run", label)
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=bool(action.get("full_page", True)))
        return str(path)

    def _collect_evidence(
        self,
        page: Any,
        browser_input: Mapping[str, Any],
        step_id: int | str | None,
        actions: list[dict[str, Any]],
        screenshot_paths: list[str],
        console_start: int,
        network_start: int,
        error: str | None,
    ) -> dict[str, Any]:
        final_url = _safe_call(lambda: page.url)
        title = _safe_call(page.title)
        dom_path, dom_summary, page_text = self._capture_dom(page, browser_input, step_id)
        media_state = self._inspect_media(page)
        return {
            "status": "fail" if error else "pass",
            "actions": actions,
            "final_url": final_url,
            "title": title,
            "screenshot_paths": screenshot_paths,
            "console": self._console_messages[console_start:],
            "failed_network": self._failed_network[network_start:],
            "dom_summary": dom_summary,
            "dom_path": dom_path,
            "page_text": page_text,
            "media_state": media_state,
            "locale": self._locale or self.locale,
            "error": error,
            "auth": {
                "mode": _browser_auth_mode(browser_input.get("auth", "none")),
            },
        }

    def inspect_selectors(self, selectors: list[str]) -> dict[str, dict[str, Any]]:
        """Return deterministic state snapshots for browser_element criteria."""

        if self._page is None:
            return {}
        states = {}
        for selector in selectors:
            locator = self._page.locator(selector)
            attached = _locator_count(locator) > 0
            visible = _locator_visible(locator, attached)
            states[selector] = {"attached": attached, "visible": visible}
        return states

    def capture_values(
        self,
        capture_map: Mapping[str, Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        """Evaluate browser-only capture sources against the current page."""

        if self._page is None:
            return {}
        values = {}
        for variable, expression in (capture_map or {}).items():
            if not isinstance(expression, Mapping):
                continue
            source = expression.get("from")
            if source == "browser_attribute":
                selector = str(expression.get("selector") or "")
                name = str(expression.get("name") or "")
                value = self._page.locator(selector).get_attribute(name)
                if value is None:
                    raise ValueError(f"missing browser attribute for {variable}: {selector} {name}")
                values[variable] = value
            elif source == "browser_eval":
                script = expression.get("script") or expression.get("expression")
                if not script:
                    raise ValueError(f"browser_eval capture {variable} requires script")
                if "args" in expression:
                    values[variable] = self._page.evaluate(str(script), expression.get("args"))
                else:
                    values[variable] = self._page.evaluate(str(script))
        return values

    def _capture_dom(
        self,
        page: Any,
        browser_input: Mapping[str, Any],
        step_id: int | str | None,
    ) -> tuple[str | None, str | None, str | None]:
        try:
            content = page.content()
        except Exception as exc:
            return None, f"DOM unavailable: {exc}", None

        label = str(browser_input.get("label") or f"step_{step_id or 'unknown'}")
        path = self._dom_path(self.run_id or "run", label)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8", errors="replace")

        summary_payload = _safe_call(lambda: page.evaluate(DOM_SUMMARY_SCRIPT))
        if isinstance(summary_payload, Mapping):
            summary = _format_dom_summary(summary_payload)
            page_text = str(summary_payload.get("text") or "")
        else:
            summary = _summarize_html(str(content))
            page_text = _html_text(str(content))
        return str(path), summary, page_text

    def _inspect_media(self, page: Any) -> dict[str, Any]:
        payload = _safe_call(lambda: page.evaluate(MEDIA_STATE_SCRIPT))
        if isinstance(payload, list):
            return {"elements": payload, "state": _aggregate_media_state(payload)}
        if isinstance(payload, Mapping):
            return dict(payload)
        return {"elements": [], "state": "none"}

    def _maybe_authenticate(
        self,
        page: Any,
        browser_input: Mapping[str, Any],
        timeout_ms: int,
    ) -> None:
        mode, username, password_text = _browser_auth_credentials(browser_input.get("auth"))
        if mode != "auto":
            return
        try:
            password_locator = page.locator("input[type='password']")
            if hasattr(password_locator, "count") and password_locator.count() < 1:
                return
            page.locator("input[name='Username'], input[autocomplete='username'], input[type='text']").fill(
                username,
                timeout=min(timeout_ms, 5000),
            )
            password_locator.fill(password_text, timeout=min(timeout_ms, 5000))
            page.locator("button[type='submit'], button:has-text('Sign In'), button:has-text('Login')").click(
                timeout=min(timeout_ms, 5000),
            )
            self._wait_for_app_idle(page, timeout_ms)
        except Exception:
            return

    def _wait_for_app_idle(self, page: Any, timeout_ms: int) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            pass

    def _resolve_url(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return self.base_url.rstrip("/") + "/web"
        if text.startswith("http://") or text.startswith("https://"):
            return text
        return urljoin(self.base_url.rstrip("/") + "/", text.lstrip("/"))

    def _screenshot_path(self, run_id: str, label: str) -> Path:
        safe_label = _safe_label(label)
        base = self.artifacts_root / run_id / "screenshots" / f"{safe_label}.png"
        return _unique_path(base)

    def _dom_path(self, run_id: str, label: str) -> Path:
        safe_label = _safe_label(label)
        base = self.artifacts_root / run_id / "browser_dom" / f"{safe_label}.html"
        return _unique_path(base)

    def _handle_console(self, message: Any) -> None:
        message_type = str(_member_value(message, "type") or "")
        if message_type not in CONSOLE_TYPES:
            return
        self._console_messages.append(
            {
                "type": message_type,
                "text": str(_member_value(message, "text") or ""),
                "location": _member_value(message, "location"),
                "timestamp": _timestamp(),
            }
        )

    def _handle_response(self, response: Any) -> None:
        status = _member_value(response, "status")
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            return
        if status_int < 400:
            return
        self._failed_network.append(
            {
                "url": str(_member_value(response, "url") or ""),
                "status": status_int,
                "status_text": str(_member_value(response, "status_text") or ""),
                "timestamp": _timestamp(),
            }
        )

    def _handle_request_failed(self, request: Any) -> None:
        failure = _member_value(request, "failure") or {}
        if callable(failure):
            failure = failure()
        self._failed_network.append(
            {
                "url": str(_member_value(request, "url") or ""),
                "error": _failure_text(failure),
                "timestamp": _timestamp(),
            }
        )


def run_browser_step(
    browser_input: Mapping[str, Any],
    *,
    base_url: str = "http://localhost:8096",
    run_id: str,
    artifacts_root: str | Path | None = None,
    step_id: int | str | None = None,
) -> dict[str, Any]:
    driver = BrowserDriver(artifacts_root=artifacts_root, base_url=base_url, run_id=run_id)
    try:
        return driver.run(browser_input, step_id=step_id)
    finally:
        driver.close()


def _browser_auth_mode(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("mode") or "none")
    return str(value or "none")


def _browser_auth_credentials(value: Any) -> tuple[str, str, str]:
    if isinstance(value, Mapping):
        mode = str(value.get("mode") or "none")
        username = str(value["username"]) if "username" in value else "admin"
        password = str(value["password"]) if "password" in value else "admin"
        return mode, username, password
    if value == "auto":
        return "auto", "admin", "admin"
    return str(value or "none"), "admin", "admin"


def _viewport(value: Any) -> dict[str, int]:
    if isinstance(value, Mapping):
        return {
            "width": int(value.get("width", DEFAULT_VIEWPORT["width"])),
            "height": int(value.get("height", DEFAULT_VIEWPORT["height"])),
        }
    return dict(DEFAULT_VIEWPORT)


def _timeout_ms(action: Mapping[str, Any], default_timeout_s: float) -> int:
    timeout_s = float(action.get("timeout_s") or default_timeout_s)
    return max(1, int(timeout_s * 1000))


def _required_selector(action: Mapping[str, Any]) -> str:
    selector = str(action.get("selector") or "")
    if not selector:
        raise ValueError(f"{action.get('type')} action requires selector")
    return selector


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label)).strip("_") or "browser"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{int(time.time() * 1000)}{suffix}")


def _safe_value_metadata(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    if isinstance(value, Mapping):
        return {"type": "object", "keys": sorted(str(key) for key in value.keys())}
    return {"type": type(value).__name__}


def _format_dom_summary(payload: Mapping[str, Any]) -> str:
    parts = [
        f"title={payload.get('title')!r}",
        f"url={payload.get('url')!r}",
    ]
    if payload.get("buttons"):
        parts.append(f"buttons={json.dumps(payload.get('buttons'), ensure_ascii=True)}")
    if payload.get("inputs"):
        parts.append(f"inputs={json.dumps(payload.get('inputs'), ensure_ascii=True, sort_keys=True)}")
    parts.append(f"media_count={payload.get('media_count', 0)}")
    if payload.get("text"):
        parts.append(f"text={payload.get('text')!r}")
    return "; ".join(parts)


def _summarize_html(content: str) -> str:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    text = _html_text(content)
    return f"title={title!r}; text={text!r}"


def _html_text(content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content)
    return re.sub(r"\s+", " ", text).strip()[:2000]


def _aggregate_media_state(elements: list[Any]) -> str:
    if not elements:
        return "none"
    if any(isinstance(item, Mapping) and item.get("error") for item in elements):
        return "errored"
    if any(isinstance(item, Mapping) and item.get("ended") for item in elements):
        return "ended"
    if any(isinstance(item, Mapping) and not item.get("paused") for item in elements):
        return "playing"
    return "paused"


def _safe_on(page: Any, event: str, handler: Any) -> None:
    try:
        page.on(event, handler)
    except Exception:
        pass


def _locator_count(locator: Any) -> int:
    try:
        if hasattr(locator, "count"):
            return int(locator.count())
    except Exception:
        return 0
    return 1


def _locator_visible(locator: Any, attached: bool) -> bool:
    if not attached:
        return False
    try:
        if hasattr(locator, "is_visible"):
            return bool(locator.is_visible())
    except Exception:
        return False
    return attached


def _safe_call(callback: Any) -> Any:
    try:
        return callback()
    except Exception:
        return None


def _member_value(obj: Any, name: str) -> Any:
    value = getattr(obj, name, None)
    if callable(value):
        return value()
    return value


def _failure_text(failure: Any) -> str:
    if isinstance(failure, Mapping):
        return str(failure.get("errorText") or failure.get("error") or failure)
    return str(failure or "")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
