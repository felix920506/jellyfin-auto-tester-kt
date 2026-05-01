# Jellyfin Auto-Tester KT

Repository scaffold for a three-stage Jellyfin issue reproduction pipeline.

## Structure

- `creatures/analysis/` - Stage 1 analysis agent files.
- `creatures/execution/` - Stage 2 execution agent files.
- `creatures/report/` - Stage 3 report agent files.
- `schemas/` - Shared JSON schemas for inter-stage messages.
- `artifacts/` - Runtime output directory; per-run artifacts are ignored by Git.
- `plans/` - Architecture and stage implementation plans.

## Running the Pipeline

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

```bash
.venv/bin/python main.py https://github.com/jellyfin/jellyfin/issues/XXXX 10.9.7
```

The entrypoint loads `terrarium.yaml`, starts the Stage 1 analysis agent, lets
the channel topology drive execution and reporting, then prints either the final
report path or the human-review queue result. Use `--json` for the structured
terminal payload.
