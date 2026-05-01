# Jellyfin Auto-Tester: Master Plan

## Overview

A 3-stage KohakuTerrarium pipeline that automates reproduction of Jellyfin GitHub issues. A maintainer provides an issue URL and a target container version; the system produces a verified, human-readable reproduction report—or a clear signal that reproduction is ambiguous.

---

## Pipeline Summary

```
[Maintainer Input]
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Stage 1: Analysis Agent                        │
│  - Reads issue + fetches context                │
│  - Outputs structured ReproductionPlan JSON     │
└───────────────────┬─────────────────────────────┘
                    │ channel: plan_ready (queue)
                    ▼
┌─────────────────────────────────────────────────┐
│  Stage 2: Execution Agent                       │
│  - Pulls Docker container (version from input)  │
│  - Executes steps, captures logs + screenshots  │
│  - Outputs ExecutionResult JSON + artifacts     │
└───────────────────┬─────────────────────────────┘
                    │ channel: execution_done (queue)
                    ▼
┌─────────────────────────────────────────────────┐
│  Stage 3: Report Agent                          │
│  - Writes clean ReproductionReport              │
│  - Re-triggers Stage 2 once using only the      │
│    written steps (verification loop)            │
│  - On pass → final report delivered             │
│  - On fail → queued for human review            │
└─────────────────────────────────────────────────┘
```

---

## KohakuTerrarium Topology

### Terrarium Recipe (`terrarium.yaml`)

```yaml
version: "1.0"

creatures:
  - name: "analysis_agent"
    config: "creatures/analysis/config.yaml"
    can_send:
      - "plan_ready"

  - name: "execution_agent"
    config: "creatures/execution/config.yaml"
    listen:
      - channel: "plan_ready"
    can_send:
      - "execution_done"

  - name: "report_agent"
    config: "creatures/report/config.yaml"
    listen:
      - channel: "execution_done"
    can_send:
      - "verification_request"
      - "final_report"
      - "human_review_queue"

channels:
  - name: "plan_ready"
    type: "queue"
  - name: "execution_done"
    type: "queue"
  - name: "verification_request"
    type: "queue"
  - name: "final_report"
    type: "broadcast"
  - name: "human_review_queue"
    type: "queue"

root_agent: "analysis_agent"
```

### Programmatic Entry Point

```python
from kohaku_terrarium import Terrarium

async def run_issue(issue_url: str, container_version: str):
    engine = await Terrarium.from_recipe("terrarium.yaml")
    analysis = engine["analysis_agent"]
    async for chunk in analysis.chat(
        f"Issue: {issue_url}\nTarget version: {container_version}"
    ):
        print(chunk, end="")
```

---

## Inter-Stage Data Schemas

### ReproductionPlan (Stage 1 → Stage 2)

```json
{
  "issue_url": "https://github.com/jellyfin/jellyfin/issues/XXXX",
  "issue_title": "string",
  "target_version": "10.9.7",
  "docker_image": "jellyfin/jellyfin:10.9.7",
  "prerequisites": [
    { "type": "media_file", "description": "...", "source": "..." }
  ],
  "environment": {
    "ports": { "host": 8096, "container": 8096 },
    "volumes": [{ "host": "/tmp/jellyfin-test", "container": "/config" }],
    "env_vars": {}
  },
  "reproduction_steps": [
    {
      "step_id": 1,
      "action": "string",
      "tool": "bash | http_request | screenshot",
      "input": {},
      "expected_outcome": "string",
      "success_criteria": "string"
    }
  ],
  "success_criteria": "string",
  "failure_indicators": ["string"],
  "confidence": "high | medium | low",
  "ambiguities": ["string"]
}
```

### ExecutionResult (Stage 2 → Stage 3)

```json
{
  "plan": { "...": "ReproductionPlan as above" },
  "run_id": "string (uuid4)",
  "is_verification": false,
  "original_run_id": "string | null",
  "container_id": "string",
  "execution_log": [
    {
      "step_id": 1,
      "action": "string",
      "stdout": "string",
      "stderr": "string",
      "exit_code": 0,
      "screenshot_path": "string | null",
      "outcome": "pass | fail | skip",
      "duration_ms": 0
    }
  ],
  "overall_result": "reproduced | not_reproduced | inconclusive",
  "artifacts_dir": "/artifacts/<run_id>/",
  "jellyfin_logs": "string",
  "error_summary": "string | null"
}
```

**Verification lineage fields:**
- `is_verification` and `original_run_id` are set in the `ReproductionPlan` by `report_writer.build_verification_plan()` before being sent to Stage 2 on the `verification_request` channel.
- Stage 2 echoes both fields verbatim from the incoming plan into the `ExecutionResult` it emits.
- The Report Agent reads `is_verification` from the `ExecutionResult`—there is no separate channel-level flag or session variable. This ensures the state marker travels with the data and is present even if the Report Agent is restarted mid-run.
```

---

## Project Directory Structure

```
jellyfin-auto-tester-kt/
├── terrarium.yaml                   # Terrarium recipe
├── main.py                          # CLI entry point
├── creatures/
│   ├── analysis/
│   │   ├── config.yaml
│   │   ├── prompts/
│   │   │   ├── system.md
│   │   │   └── context.md
│   │   └── tools/
│   │       └── github_fetcher.py
│   ├── execution/
│   │   ├── config.yaml
│   │   ├── prompts/
│   │   │   └── system.md
│   │   └── tools/
│   │       ├── docker_manager.py
│   │       ├── jellyfin_api.py
│   │       └── screenshot.py
│   └── report/
│       ├── config.yaml
│       ├── prompts/
│       │   └── system.md
│       └── tools/
│           └── report_writer.py
├── schemas/
│   ├── reproduction_plan.json
│   └── execution_result.json
├── artifacts/                       # Runtime: per-run subdirs created here
└── plans/
    ├── plan-master.md               # This file
    ├── plan-stage1-analysis.md
    ├── plan-stage2-execution.md
    └── plan-stage3-report.md
```

---

## Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| Issue is underspecified | Analysis Agent emits `confidence: low` + `ambiguities` list; pipeline halts with human prompt |
| Docker pull fails | Execution Agent retries 3×; if unavailable, reports clearly |
| Reproduction is environment-dependent | Execution Agent captures full `docker inspect` + system info |
| Verification loop fails | Report Agent routes to `human_review_queue`; does not re-loop a second time |
| Container hangs | Execution Agent enforces per-step timeouts via subprocess with `timeout` |

---

## Key Design Decisions

- **One verification loop only.** The report agent re-runs Stage 2 exactly once using only the written steps. A second failure queues for human review rather than looping again—prevents runaway costs.
- **Maintainer specifies version.** Docker image version is always a human-provided input, never inferred by the agent, to avoid false reproductions against wrong versions.
- **Artifacts are stored locally.** All screenshots, logs, and outputs land in `/artifacts/<run-uuid>/` so every run is independently reviewable.
- **Channel-based decoupling.** Stages communicate via KohakuTerrarium queue channels, not direct function calls, so each stage can be run and debugged independently.
