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

The entrypoint loads `terrarium.yaml`, starts the Stage 1 analysis agent, lets
the channel topology drive execution and reporting, then prints either the final
report path or the human-review queue result. LLM transcripts are written to the
stage log files instead of streamed to the terminal. Use `--json` for the
structured terminal payload.

KohakuTerrarium and pipeline logs are mirrored to stderr by default. Use
`--log-level DEBUG` for more detail, `--log-stderr off` to keep logs file-only,
or set `JF_AUTO_TESTER_LOG_LEVEL` / `JF_AUTO_TESTER_LOG_STDERR` in `.env`.

## Running Stages Individually

The full pipeline remains channel-driven. For debugging, each stage can also be
run with disk handoff folders:

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

Stage 1 writes `transcript.json` and `plan.json`, Stage 2 reads `plan.json`
and writes `result.json`, and Stage 3 reads `result.json` and writes `report.md` plus
`verification_plan.json`. To debug the verification pass, feed that plan back
through Stage 2 and then finalize the report:

```bash
.venv/bin/python main.py --stage execution \
  --input debug/stage3/verification_plan.json \
  --out debug/stage2-verify

.venv/bin/python main.py --stage report \
  --input debug/stage2 \
  --verification-result debug/stage2-verify \
  --out debug/stage3-final
```
