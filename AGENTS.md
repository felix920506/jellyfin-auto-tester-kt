# Repository Guidelines

## Project Structure & Module Organization

This repository implements a three-stage Jellyfin issue reproduction pipeline. `main.py` is the CLI entrypoint and pipeline fabric. Reusable Python tools live in `tools/`, including Docker, GitHub, Jellyfin API, screenshot, criteria, execution, and report helpers. Stage agent configuration and prompts are under `creatures/analysis/`, `creatures/execution/`, and `creatures/report/`. Shared JSON contracts are in `schemas/`, implementation notes are in `plans/`, and runtime output belongs in `artifacts/` or `debug/`. Tests are in `tests/` and generally mirror the tool or pipeline behavior they cover.

## Build, Test, and Development Commands

Create and prepare a local environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

Run the full pipeline:

```bash
.venv/bin/python main.py https://github.com/jellyfin/jellyfin/issues/XXXX 10.9.7
```

Run stages individually with disk handoffs:

```bash
.venv/bin/python main.py --stage analysis URL VERSION --out debug/stage1
.venv/bin/python main.py --stage execution --input debug/stage1 --out debug/stage2
.venv/bin/python main.py --stage web-client --input debug/stage1 --out debug/stage2
.venv/bin/python main.py --stage report --input debug/stage2 --out debug/stage3
```

Run tests with the standard library:

```bash
.venv/bin/python -m unittest discover tests
```

## Coding Style & Naming Conventions

Use Python 3 style with four-space indentation, type hints where they clarify interfaces, and `pathlib.Path` for filesystem paths. Keep module names snake_case, classes CapWords, functions and variables snake_case, and constants UPPER_SNAKE_CASE. Prefer focused helpers in `tools/` over adding logic directly to prompts or CLI branches. Keep comments short and reserve them for non-obvious behavior.

## Testing Guidelines

Tests use `unittest` and fake collaborators for Docker, API, screenshots, and command execution. Name files `test_<module>.py`, test classes `<Feature>Tests`, and methods `test_<behavior>`. Add focused tests when changing stage handoff contracts, criteria evaluation, filesystem artifacts, Docker/API behavior, or report generation.

## Commit & Pull Request Guidelines

Recent commits use concise, imperative summaries such as `Add debug logging to github tool wrappers` and `Centralize stage tools into top-level tools/ package`. Keep the first line specific and under roughly 72 characters when possible. Pull requests should explain the behavior change, list verification commands run, reference related Jellyfin or repo issues, and include sample output or screenshots when report or transcript-viewer behavior changes.

## Security & Configuration Tips

Do not commit `.env`, provider API keys, run artifacts, or debug outputs. Prefer `kt login <provider>` for KohakuTerrarium credentials; only place provider keys in `.env` when this process should supply auth. Use `GITHUB_TOKEN` locally to avoid low GitHub rate limits.
