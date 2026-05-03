# Stage 2: Execution

Owns Docker setup, deterministic step execution, evidence capture, and `ExecutionResult` emission.

See `plans/stage2-execution/plan.md` for the detailed implementation plan.

Screenshot captures run Chromium with a visible GUI by default when the host
appears to have a display. Leave `JF_AUTO_TESTER_BROWSER_HEADLESS` unset, or set
it to `auto`, for this default. Set it to `true` to force headless mode, or
`false` to force a visible browser.

## Browser Driver

Use `tool: "browser"` for Jellyfin Web UI flows. The browser driver keeps one
Chromium browser, context, and page for the run, so multiple browser steps share
session state. Relative `path` values resolve against the Jellyfin base URL that
Docker startup returns.

Browser input fields:

- `path` or `url`: target used by `goto` actions when the action does not supply
  its own target.
- `auth`: `auto` attempts the real Jellyfin Web login with `admin` / `admin` when
  a login form is detected; `none` leaves the page unauthenticated.
- `label`, `timeout_s`, `viewport`: artifact naming, default action timeout, and
  Chromium viewport.
- `actions`: ordered DSL actions. Supported actions are `goto`, `refresh`,
  `click`, `fill`, `press`, `select_option`, `check`, `uncheck`, `wait_for`,
  `wait_for_text`, `wait_for_url`, `wait_for_media`, `evaluate`, and
  `screenshot`.

`refresh` reloads the current page and waits for app idle. Browser results record
each action's status, duration, selector, safe value metadata, error text, final
URL/title, screenshots, console warnings/errors, failed network responses, DOM
summary and full DOM artifact, and media state.

The repair-loop API (`start_plan`, `retry_browser_step`, `finalize_plan`) allows
one retry per failed browser step. The retry may change only that browser step's
input (`actions`, selectors, path/url, waits, labels, viewport, and explicit
`refresh`). `execute_plan` remains the deterministic one-attempt compatibility
path used by file-based debug runs.
