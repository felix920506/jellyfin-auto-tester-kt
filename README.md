# Jellyfin Auto-Tester KT

Repository scaffold for a three-stage Jellyfin issue reproduction pipeline.

## Structure

- `creatures/analysis/` - Stage 1 analysis agent files.
- `creatures/execution/` - Stage 2 execution agent files.
- `creatures/report/` - Stage 3 report agent files.
- `tools/` - Reusable Python utilities for Docker, GitHub, and Jellyfin API.
- `utils/` - Manual verification utilities, including browser replay and the transcript viewer.
- `schemas/` - Shared JSON schemas for inter-stage messages.
- `artifacts/` - Runtime output directory (ignored by Git).
- `debug/` - Default output directory for manual stage execution.
- `plans/` - Architecture and stage implementation plans.
- `tests/` - Unit tests for tools and pipeline fabric.

## AI Use Disclosure

This project was developed with the assistance of AI tools, including Codex, Claude, and Gemini.

## Authentication

This project relies on [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium) for LLM orchestration. Authenticate your providers using the `kt login` flow:

```bash
.venv/bin/kt login openrouter
```

For more details on authentication, refer to the [KohakuTerrarium documentation](https://github.com/Kohaku-Lab/KohakuTerrarium/blob/main/docs/en/guides/getting-started.md#3-authenticate-a-model-provider).

The CLI also loads `.env` automatically for project runtime settings. Provider API keys (e.g., `OPENROUTER_API_KEY`) should only be added to `.env` if you want to bypass the KohakuTerrarium saved login store for a specific environment.

## Running the Pipeline

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

```bash
.venv/bin/python main.py https://github.com/jellyfin/jellyfin/issues/XXXX 10.9.7
```

## Transcript Viewer

The repository includes a standalone web application to view execution transcripts:

1. Open `utils/transcript-viewer/index.html` in a web browser.
2. Click **"Select Debug Folder"** and choose the `debug/` directory.
3. Select a transcript from the sidebar to view the conversation and tool calls.

Debug runs write model traffic to `transcript.json` and the surrounding run
details to `transcript_metadata.json`.

## Running Tests

Tests use the standard library `unittest` framework:

```bash
.venv/bin/python -m unittest discover tests
```

The entrypoint loads `terrarium.yaml`, starts the Stage 1 analysis agent, lets
the channel topology drive execution and reporting, then prints either the final
report path or the human-review queue result. LLM transcripts are written to the
stage log files instead of streamed to the terminal. Use `--json` for the
structured terminal payload.

KohakuTerrarium and pipeline logs are mirrored to stderr by default. Use
`--log-level DEBUG` for more detail, `--log-stderr off` to keep logs file-only,
or set `JF_AUTO_TESTER_LOG_LEVEL` / `JF_AUTO_TESTER_LOG_STDERR` in `.env`.
Stage 1 refuses to start with models blocked by
`stage1_model_blacklist.py`; edit `is_stage1_model_blacklisted()` there when the
model blocklist needs to change.

## Running Stages Individually

The full pipeline remains channel-driven through explicit KohakuTerrarium
`send_message` calls. For debugging, each stage can also be run with disk
handoff folders:

### Key Environment Variables

- `GITHUB_TOKEN`: Recommended to avoid GitHub API rate limits.
- `JF_AUTO_TESTER_LOG_LEVEL`: Controls logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- `JF_AUTO_TESTER_LOG_STDERR`: Controls whether logs are mirrored to stderr (`on`, `off`, `auto`).
- `JF_AUTO_TESTER_BROWSER_HEADLESS`: Controls browser visibility in Stage 2.
  Leave unset or use `auto` to show a GUI browser when the host has a display,
  `true` to force headless mode, or `false` / `headed` / `gui` to force a
  visible browser.

### Browser Steps

Stage 2 supports `tool: "browser"` for Jellyfin Web flows that need a real
Chromium client. Browser input supports top-level `path` or `url`, `auth`,
`label`, `timeout_s`, `viewport`, and ordered `actions`.

For pure web-client plans that need no server control, custom media, admin
settings, API setup, or logs, `server_target.mode: "demo"` can run browser
steps against `https://demo.jellyfin.org/stable` or
`https://demo.jellyfin.org/unstable`. Demo login uses username `demo` and a
blank password. Browser `auth` may be `"auto"`, `"none"`, or a credential object
such as `{"mode": "auto", "username": "demo", "password": ""}`.

Supported action types are `goto`, `refresh`, `click`, `fill`, `press`,
`select_option`, `check`, `uncheck`, `wait_for`, `wait_for_text`,
`wait_for_url`, `wait_for_media`, `evaluate`, and `screenshot`. `refresh` is an
explicit action that reloads the current page and then waits for app idle.
`click` accepts either a CSS `selector` or visible `text`. `wait_for_media`
accepts `playing`, `paused`, `ended`, `errored`, `none`, or `stopped`.
Screenshots from browser steps are written under the run's `screenshots/`
artifact directory and can satisfy `screenshot_present`.

Browser-specific criteria include `browser_action_run`, `browser_element`,
`browser_text_contains`, `browser_url_matches`, `browser_media_state`, and
`browser_console_matches`. Browser capture sources include `browser_text`,
`browser_url`, `browser_attribute`, and `browser_eval`.

When the repair-loop API is used, Stage 2 may retry a failed browser step once.
The repair can only change that browser step's input fields (`actions`, selectors
inside actions, path/url, waits, labels, viewport, and explicit `refresh`). It
cannot change prerequisites, Docker image, non-browser steps, roles, expected
outcomes, or success criteria.

Every `web_client_session` run writes replay artifacts under
`<run>/browser_replay/`: `replay_manifest.json`, `replay_browser_session.py`,
`README.md`, and `original_trace.zip` when Playwright tracing is available.
Run the generated script to re-execute accepted browser actions, or inspect the
trace with `playwright show-trace`.

### Browser Replay Utility

Browser replay is for manual verification of a `web_client_session` run. It
does not read `transcript.json`; it replays accepted browser actions from
`browser_replay/replay_manifest.json` in one fresh Playwright browser session.
Schema-invalid calls, `start`, `advance_step`, and `finalize` are printed as
skipped audit events.

Run the utility directly with a manifest:

```bash
.venv/bin/python -m utils.browser_replay \
  artifacts/RUN_ID/browser_replay/replay_manifest.json \
  --base-url http://localhost:8096 \
  --headless true \
  --slow-mo-ms 250 \
  --stop-on-failure
```

For example, this replays the actions captured in `debug/stage2web-test5-7`:

```bash
.venv/bin/python -m utils.browser_replay \
  debug/stage2web-test5-7/web-client-60400220-d40a-4af9-91fd-3b88f909d4cf/browser_replay/replay_manifest.json \
  --base-url https://demo.jellyfin.org/stable \
  --headless true \
  --stop-on-failure
```

New replay artifact directories also include a generated convenience script:

```bash
.venv/bin/python artifacts/RUN_ID/browser_replay/replay_browser_session.py \
  --base-url http://localhost:8096 \
  --headless true
```

Older artifacts generated before the replay utility moved to `utils/` may have
a stale generated-script import. In that case, use the `python -m
utils.browser_replay .../replay_manifest.json` form above.

If `--base-url` is omitted, replay uses the manifest's original base URL. The
target Jellyfin server must already be running and reachable. Replay output is
written to `browser_replay/replay-runs/<timestamp>/` with
`action_result_log.json`, fresh screenshots/DOM captures under that replay run,
`replay_result.json`, and `replay_trace.zip` when tracing is available.

To inspect the original visual trace instead of re-executing actions:

```bash
.venv/bin/python -m playwright show-trace \
  artifacts/RUN_ID/browser_replay/original_trace.zip
```

For the `debug/stage2web-test5-7` example trace:

```bash
.venv/bin/python -m playwright show-trace \
  debug/stage2web-test5-7/web-client-60400220-d40a-4af9-91fd-3b88f909d4cf/browser_replay/original_trace.zip
```

```bash
.venv/bin/python main.py --stage analysis URL VERSION --out debug/stage1

.venv/bin/python main.py --stage execution \
  --input debug/stage1 \
  --out debug/stage2

.venv/bin/python main.py --stage web-client \
  --input debug/stage1 \
  --out debug/stage2

.venv/bin/python main.py --stage report \
  --input debug/stage2 \
  --out debug/stage3
```

- **Stage 1 (Analysis)** writes `transcript.json`, `transcript_metadata.json`, and `plan.md`.
- **Stage 2 (Execution)** reads `plan.md` and writes `execution_result.json`.
- **Stage 2 (Web Client)** is a peer to Execution for pure Jellyfin Web bugs; it reads `plan.md` and writes `transcript.json`, `transcript_metadata.json`, and `execution_result.json`.
- **Stage 3 (Report)** reads `execution_result.json`, runs the report and verification agents through KT channels, and writes `transcript.json`, `transcript_metadata.json`, `report.md`, plus `final_report.json` or `human_review_queue.json`.

For compatibility with an already captured verification run, pass it with
`--verification-result`; Stage 3 still waits for the report agent to request
verification before injecting that supplied result:

```bash
.venv/bin/python main.py --stage report \
  --input debug/stage2 \
  --verification-result debug/stage2-verify \
  --out debug/stage3-final
```
