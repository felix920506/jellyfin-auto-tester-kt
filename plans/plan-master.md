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
└───────┬───────────────────────────────┬─────────┘
        │ channel: plan_ready           │ channel: web_client_plan_ready
        ▼                               ▼
┌───────────────────────────┐   ┌───────────────────────────┐
│  Stage 2: Execution Agent │   │  Stage 2: Web Client Agent│
│  - Pulls Docker container │   │  - Pure browser execution │
│  - Executes steps         │   │  - Demo server or Tasks   │
└───────┬───────────────────┘   └───────┬───────────────────┘
        │                               │
        │ channel: execution_done (queue)│
        └───────────────┬───────────────┘
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
    base_config: "creatures/analysis"
    channels:
      can_send:
        - "plan_ready"
        - "web_client_plan_ready"

  - name: "execution_agent"
    base_config: "creatures/execution"
    channels:
      listen:
        - "plan_ready"
        - "verification_request"
      can_send:
        - "execution_done"

  - name: "web_client_agent"
    base_config: "creatures/web_client"
    channels:
      listen:
        - "web_client_plan_ready"
        - "web_client_verification_request"
        - "web_client_task"
      can_send:
        - "execution_done"
        - "web_client_done"

  - name: "report_agent"
    base_config: "creatures/report"
    channels:
      listen:
        - "execution_done"
      can_send:
        - "verification_request"
        - "web_client_verification_request"
        - "final_report"
        - "human_review_queue"

channels:
  plan_ready:
    type: "queue"
  web_client_plan_ready:
    type: "queue"
  web_client_verification_request:
    type: "queue"
  execution_done:
    type: "queue"
  web_client_task:
    type: "queue"
  web_client_done:
    type: "queue"
  verification_request:
    type: "queue"
  final_report:
    type: "broadcast"
  human_review_queue:
    type: "queue"

root_agent: "analysis_agent"
```

### Programmatic Entry Point

```python
import json

from kohaku_terrarium import Terrarium
from creatures.analysis.tools.github_fetcher import github_fetcher

async def run_issue(issue_url: str, container_version: str):
    engine = await Terrarium.from_recipe("terrarium.yaml")
    analysis = engine["analysis_agent"]
    issue_thread = github_fetcher(issue_url, include_comments=True, include_linked=True)
    async for chunk in analysis.chat(
        f"Issue: {issue_url}\n"
        f"Target version: {container_version}\n\n"
        f"Prefetched GitHub issue thread JSON:\n{json.dumps(issue_thread)}"
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
      "role": "setup | trigger | verify",
      "action": "string",
      "tool": "bash | http_request | screenshot | docker_exec | browser",
      "input": {},
      "capture": {
        "item_id": { "from": "body_json_path", "path": "$.Items[0].Id" }
      },
      "expected_outcome": "string",
      "success_criteria": {
        "all_of": [
          { "type": "status_code", "equals": 500 },
          { "type": "body_contains", "value": "Transcoding failed" },
          { "type": "log_matches", "pattern": "HEVC decode error" },
          { "type": "exit_code", "equals": 0 }
        ]
      }
    }
  ],
  "reproduction_goal": "string",
  "failure_indicators": ["string"],
  "confidence": "high | medium | low",
  "ambiguities": ["string"],
  "is_verification": false,
  "original_run_id": null
}
```

**Step `role` semantics:**
- `setup` — prerequisite action; expected to pass cleanly
- `trigger` — the action that causes the bug; its `expected_outcome` describes the observable failure symptom (e.g. HTTP 500, error in logs, wrong UI state)
- `verify` — optional post-trigger assertion to confirm the failure state

Exactly one step must have `role: "trigger"`. Stage 2 uses this to determine `overall_result` without scanning all logs or counting pass rates.

For `trigger` steps, `success_criteria` deliberately describes observing the bug symptom (e.g. "response contains 'Transcoding failed'"). A `pass` on a trigger step means the defect manifested as expected; a `fail` means it did not appear. Stage 2 applies the same pass/fail evaluation uniformly to all steps—no special-casing.

**`http_request` input contract:**

`http_request` is a raw Jellyfin HTTP transport, not a spec-compliant Jellyfin SDK. Use it for normal API calls and for intentionally non-standard requests that can still be represented through structured HTTP fields. Use `bash`/`curl` only for malformed HTTP framing that the transport cannot express, such as deliberately wrong `Content-Length`.

```json
{
  "method": "POST",
  "path": "/Items/${item_id}/PlaybackInfo",
  "params": { "Recursive": "true" },
  "headers": { "Content-Type": "application/json" },
  "auth": "auto",
  "body_json": { "DeviceProfile": { "MaxStreamingBitrate": 2000000 } },
  "timeout_s": 30,
  "follow_redirects": false,
  "allow_absolute_url": false
}
```

Required fields are `method`, `path`, and `auth`. `auth` is one of `auto` (use the Stage 2 admin token), `none` (send no token), or `token` (send the supplied `token`). Use at most one body field: `body_json`, `body_text`, or `body_base64`. Do not use a generic `body` field. Set `Content-Type` explicitly when it matters, including for malformed JSON sent with `body_text`, e.g. `"body_text": "{\"invalid\":"`; use `body_base64` for raw bytes, e.g. `"body_base64": "AAEC"`.

**`browser` input contract:**

Use `browser` for Jellyfin Web UI, React state, media playback, and client/server
interaction bugs that require a real Chromium client.

```json
{
  "path": "/web/index.html",
  "auth": "auto",
  "label": "playback_flow",
  "timeout_s": 30,
  "viewport": { "width": 1280, "height": 720 },
  "actions": [
    { "type": "goto" },
    { "type": "click", "selector": ".play-button" },
    { "type": "wait_for_media", "state": "playing" },
    { "type": "screenshot", "label": "playback_flow" }
  ]
}
```

Supported browser actions are `goto`, `refresh`, `click`, `fill`, `press`,
`select_option`, `check`, `uncheck`, `wait_for`, `wait_for_text`,
`wait_for_url`, `wait_for_media`, `evaluate`, and `screenshot`. `refresh`
reloads the current page and waits for app idle. `click` accepts either a CSS
`selector` or visible `text`; `wait_for_media` accepts `playing`, `paused`,
`ended`, `errored`, `none`, or `stopped`.

**`success_criteria` evaluation (deterministic, no LLM):**

Per-step `success_criteria` is a structured object — never free text — so Stage 2 can evaluate it programmatically and produce reproducible outcomes. The shape is `{ "all_of": [<assertion>, ...] }` or `{ "any_of": [<assertion>, ...] }` (mutually exclusive at the top level; nested combinators are not supported in v1).

Supported assertion types:

| `type`           | Fields                          | Meaning |
|---|---|---|
| `status_code`    | `equals: int` or `in: [int]`    | HTTP response status (http_request steps only) |
| `body_contains`  | `value: string`                 | Substring match against response body |
| `body_matches`   | `pattern: string`               | Python regex against response body |
| `body_json_path` | `path: string`, `equals: any`   | JSONPath into response body equals value |
| `exit_code`      | `equals: int` or `in: [int]`    | Process/docker_exec exit code |
| `stdout_contains`| `value: string`                 | Substring in stdout |
| `stderr_contains`| `value: string`                 | Substring in stderr |
| `log_matches`    | `pattern: string`, `since_step_start: bool = true` | Regex against `docker logs` since step began |
| `screenshot_present` | `label: string`             | A screenshot was captured under this label |
| `browser_action_run` | none                       | Browser step completed all actions successfully |
| `browser_element` | `selector: string`, `state: exists\|visible\|hidden\|attached\|detached` | Browser selector state |
| `browser_text_contains` | `value: string`        | Browser page text contains a string |
| `browser_url_matches` | `pattern: string`        | Regex against final browser URL |
| `browser_media_state` | `state: playing\|paused\|ended\|errored\|none\|stopped` | Aggregated media state |
| `browser_console_matches` | `pattern: string`    | Regex against captured console warnings/errors |

A step passes iff its `success_criteria` evaluates to true under this DSL. There is no LLM-based judgment in the loop; the agent's job is to dispatch the tool call, not to interpret the result. The top-level `reproduction_goal` is human-readable context only and must not be used by Stage 2 for pass/fail decisions.

**Step variable binding (`capture` + `${var}` interpolation):**

Steps may declare a `capture` map: `{ "<var_name>": { "from": <source>, ... } }`. After the step runs, each entry is evaluated against the step's result and stored in a per-run variable scope. Subsequent steps can reference the variable anywhere inside their `input` (and inside `success_criteria` value/pattern fields) using `${var_name}`. Interpolation is string-substitution; nested expressions are not supported.

Supported `from` sources mirror the assertion DSL:

| `from`              | Extra fields            | Returns |
|---|---|---|
| `body_json_path`    | `path: string`          | Value at JSONPath in response body |
| `body_regex`        | `pattern: string`, `group: int = 1` | Capture group from response body regex |
| `header`            | `name: string`          | HTTP response header value |
| `stdout_regex`      | `pattern: string`, `group: int = 1` | Capture group from stdout |
| `stdout_trimmed`    | (none)                  | Whole stdout, stripped |
| `exit_code`         | (none)                  | Integer exit code |
| `browser_text`      | (none)                  | Browser page text |
| `browser_url`       | (none)                  | Final browser URL |
| `browser_attribute` | `selector: string`, `name: string` | Attribute value from a browser element |
| `browser_eval`      | `script: string` or `expression: string`, `args: any` | Result of a browser JavaScript evaluation |

Resolution rules: variables are scoped to the run; later steps overwrite earlier captures with the same name; referencing an undefined variable marks the step `fail` with reason `"unbound variable: <name>"`. Capture failures (e.g. JSONPath misses) mark the step `fail` with reason `"capture failed: <var>"` and do not bind the variable.

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
      "role": "setup | trigger | verify",
      "action": "string",
      "tool": "bash | http_request | screenshot | docker_exec | browser",
      "stdout": "string",
      "stderr": "string",
      "exit_code": 0,
      "http": {
        "status_code": 200,
        "body": "string | null",
        "headers": {}
      },
      "screenshot_path": "string | null",
      "outcome": "pass | fail | skip",
      "reason": "string | null",
      "criteria_evaluation": {
        "passed": true,
        "assertions": [
          {
            "type": "status_code",
            "passed": true,
            "actual": 200,
            "expected": 200,
            "message": "string"
          }
        ]
      },
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

### WebClientTask (Execution Agent → Web Client Agent)

Used for interactive browser delegation during a standard execution run.

```json
{
  "command": "start | action | finalize",
  "request_id": "string",
  "session_id": "string | null",
  "run_id": "string | null",
  "base_url": "string | null",
  "artifacts_root": "string | null",
  "browser_input": {
    "path": "string",
    "auth": "auto | none | object",
    "label": "string",
    "timeout_s": 30,
    "viewport": { "width": 1280, "height": 720 }
  },
  "action": {
    "type": "goto | click | fill | ...",
    "selector": "string",
    "value": "any",
    "label": "string"
  },
  "selector_assertions": [
    { "selector": ".element", "state": "visible" }
  ],
  "capture": {
    "var_name": { "from": "browser_text" }
  }
}
```

### WebClientResult (Web Client Agent → Execution Agent)

```json
{
  "request_id": "string",
  "status": "pass | fail | error",
  "browser": {
    "url": "string",
    "text": "string",
    "console": []
  },
  "screenshot_path": "string | null",
  "browser_screenshots": { "label": "path" },
  "selector_states": { ".element": { "visible": true } },
  "capture_values": { "var_name": "value" },
  "error": "string | null"
}
```

**`overall_result` derivation (Stage 2 responsibility):**
- `reproduced`: the `trigger` step's outcome is `pass` — its `success_criteria` (the bug symptom) was observed
- `not_reproduced`: the `trigger` step's outcome is `fail` — the bug symptom was not observed
- `inconclusive`: the `trigger` step could not be reached (container crash, prerequisite failure, timeout) OR no step has `role: "trigger"`

**Browser repair loop:**

`execute_plan(plan)` remains the deterministic one-attempt path. The optional
repair API uses `start_plan(plan)`, `retry_browser_step(step_id, browser_input)`,
and `finalize_plan()`. A failed browser step may be retried exactly once. Repair
can change only that failed browser step's input (`actions`, selectors, path/url,
waits, labels, viewport, and explicit `refresh`) and cannot change
prerequisites, Docker image, non-browser steps, roles, expected outcomes, or
success criteria.

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
│   ├── web_client/
│   │   ├── config.yaml
│   │   ├── README.md
│   │   └── prompts/
│   │       └── system.md
│   └── report/
│       ├── config.yaml
│       ├── prompts/
│       │   └── system.md
├── tools/
│   ├── docker_manager.py
│   ├── jellyfin_http.py
│   ├── screenshot.py
│   ├── browser.py
│   ├── criteria.py
│   ├── execution_runner.py
│   ├── github_search.py
│   ├── report_writer.py
│   ├── web_client_delegate.py
│   └── web_client_runner.py
├── schemas/
│   ├── reproduction_plan.json
│   ├── execution_result.json
│   ├── web_client_task.json
│   └── web_client_result.json
├── artifacts/                       # Runtime: per-run subdirs created here
└── plans/
    ├── plan-master.md               # This file
    ├── stage1-analysis/
    │   └── plan.md
    ├── stage2-execution/
    │   └── plan.md
    └── stage3-report/
        └── plan.md
```

---

## Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| Issue is underspecified | Analysis Agent emits `confidence: low` + `ambiguities` list; pipeline halts with human prompt |
| Docker pull fails | Execution Agent retries 3×; if unavailable, reports clearly |
| Reproduction is environment-dependent | Execution Agent captures full `docker inspect` + system info |
| Verification loop fails | Report Agent routes to `human_review_queue`; does not re-loop a second time |
| Container hangs | Execution Agent enforces per-step timeouts via `docker_manager.exec(timeout_s=120)`; the Docker SDK raises `APIError` on expiry, which marks the step `fail` and triggers teardown |

---

## Key Design Decisions

- **One verification loop only.** The report agent re-runs Stage 2 exactly once using only the written steps. A second failure queues for human review rather than looping again—prevents runaway costs.
- **Maintainer specifies version.** Docker image version is always a human-provided input, never inferred by the agent, to avoid false reproductions against wrong versions.
- **Artifacts are stored locally.** All screenshots, logs, and outputs land in `/artifacts/<run-uuid>/` so every run is independently reviewable.
- **Channel-based decoupling.** Stages communicate via KohakuTerrarium queue channels using explicit `send_message` calls, not direct function calls or named output blocks, so each stage can be run and debugged independently.

## Channel Consumers

| Channel | Producer | Consumer |
|---|---|---|
| `plan_ready` | analysis_agent | execution_agent (auto-trigger) |
| `web_client_plan_ready` | analysis_agent | web_client_agent (auto-trigger) |
| `execution_done` | execution_agent, web_client_agent | report_agent (auto-trigger) |
| `verification_request` | report_agent | execution_agent (auto-trigger) |
| `web_client_verification_request` | report_agent | web_client_agent (auto-trigger) |
| `web_client_task` | execution_agent | web_client_agent (auto-trigger) |
| `web_client_done` | web_client_agent | execution_agent (auto-trigger) |
| `final_report` | report_agent | `main.py` CLI prints the report path; no agent listens |
| `human_review_queue` | report_agent | **No automated consumer.** Inspect manually with `kt channel inspect human_review_queue`. |
