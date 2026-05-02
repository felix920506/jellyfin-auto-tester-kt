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

## Running the Pipeline

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

Authenticate LLM providers with KohakuTerrarium's normal login flow, for
example `.venv/bin/kt login openrouter` for the OpenRouter presets. The CLI
loads `.env` automatically when present for project runtime settings, without
overriding variables already exported in the shell. Provider API keys should
only be added to `.env` when you explicitly want this process environment to
supply provider auth instead of relying on KohakuTerrarium's saved login store.

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

explicitly want this process environment to supply provider auth instead of
relying on KohakuTerrarium's saved login store.

### Key Environment Variables

- `GITHUB_TOKEN`: Recommended to avoid GitHub API rate limits.
- `JF_AUTO_TESTER_LOG_LEVEL`: Controls logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- `JF_AUTO_TESTER_LOG_STDERR`: Controls whether logs are mirrored to stderr (`on`, `off`, `auto`).
- `JF_AUTO_TESTER_BROWSER_HEADLESS`: Controls browser visibility in Stage 2 (`true`, `false`, `auto`).

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
