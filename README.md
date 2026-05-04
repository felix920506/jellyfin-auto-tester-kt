# Jellyfin Auto-Tester KT

Repository scaffold for a three-stage Jellyfin issue reproduction pipeline.

## Structure

- `creatures/analysis/` - Stage 1 analysis agent files.
- `creatures/execution/` - Stage 2 execution agent files.
- `creatures/report/` - Stage 3 report agent files.
- `tools/` - Reusable Python utilities for Docker, GitHub, and Jellyfin API.
- `transcript-viewer/` - Standalone web app for viewing execution transcripts.
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

1. Open `transcript-viewer/index.html` in a web browser.
2. Click **"Select Debug Folder"** and choose the `debug/` directory.
3. Select a transcript from the sidebar to view the conversation and tool calls.

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

Supported action types are `goto`, `refresh`, `click`, `fill`, `press`,
`select_option`, `check`, `uncheck`, `wait_for`, `wait_for_text`,
`wait_for_url`, `wait_for_media`, `evaluate`, and `screenshot`. `refresh` is an
explicit action that reloads the current page and then waits for app idle.
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

```bash
.venv/bin/python main.py --stage analysis \
  https://github.com/jellyfin/jellyfin/issues/XXXX 10.9.7 \
  --out debug/stage1

.venv/bin/python main.py --stage execution \
  --input debug/stage1 \
  --out debug/stage2

.venv/bin/python main.py --stage report \
  --input debug/stage2 \
  --out debug/stage3
```

- **Stage 1 (Analysis)** writes `transcript.json` and `reproduction_plan.json`.
- **Stage 2 (Execution)** reads `reproduction_plan.json` and writes `execution_result.json`.
- **Stage 3 (Report)** reads `execution_result.json` and writes `report.md` plus `verification_plan.json`.

To debug the verification pass, feed that plan back through Stage 2 and then finalize the report:

```bash
.venv/bin/python main.py --stage execution \
  --input debug/stage3/verification_plan.json \
  --out debug/stage2-verify

.venv/bin/python main.py --stage report \
  --input debug/stage2 \
  --verification-result debug/stage2-verify \
  --out debug/stage3-final
```
