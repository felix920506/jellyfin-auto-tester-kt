# Program-Owned Execution Results Plan

## Problem

Artifacts in `debug/stage2web-test5-5` show that the Web Client Agent is doing
two jobs that should belong to the program:

- It has to remember the browser actions it already took, including failed
  fallback attempts.
- It has to choose and restate the final reproduction result after a long
  sequence of tool calls.

The run has 24 `execution_log` entries for a six-step plan, including five
entries labeled as trigger step `5`. Some trigger entries are exploratory
failures, some are successful UI setup actions, and one is a failed wait. The
top-level `overall_result` is supplied during `finalize`, so the final report
depends on the model's end-of-run recall instead of a canonical program-owned
summary.

The existing per-action files already contain enough raw data. The missing
piece is a deterministic state layer that turns those raw action attempts into
the authoritative execution result.

## Goals

- Make the program the source of truth for what was attempted, what evidence was
  captured, which planned step each attempt belongs to, and which attempt is
  decisive.
- Keep raw chronological action attempts for debugging, but add a compact
  program-generated step summary for report generation.
- Derive `overall_result` from the trigger step summary and evaluated criteria,
  not from a model-supplied `finalize` value.
- Allow the Web Client Agent to continue choosing the next browser action from
  current page state, without requiring it to remember prior actions in the
  final response.
- Preserve the existing `ExecutionResult` shape enough that old consumers can
  still inspect `execution_log`, while making new fields authoritative.

## Non-Goals

- Do not remove exploratory browser control from the Web Client Agent.
- Do not require fully scripted browser plans before the first exploratory run.
- Do not make Stage 3 reinterpret raw browser DOM, transcripts, or sidecar
  action files to decide reproduction status.
- Do not add a remote observability backend.

## Off-the-Shelf Options

The closest library options are structured logging or JSON Lines helpers:

- `jsonlines` simplifies reading and writing JSON Lines files, with UTF-8
  handling, validation/error behavior, and optional alternate JSON backends.
  Source: https://jsonlines.readthedocs.io/
- `structlog` is a production structured logging library with JSON output and
  standard-library logging integration. Source: https://www.structlog.org/
- OpenTelemetry logs provide a common telemetry model and export path, but the
  Python logging setup is aimed at collection/export, not local domain-specific
  run state. Source:
  https://opentelemetry.io/docs/languages/python/instrumentation/

Recommendation for this repository: use an in-repo implementation over the
standard library `json` module. The data is not generic application telemetry;
it is domain state with explicit invariants, schemas, artifact paths, and
step/attempt relationships. `jsonlines` is the only close dependency, but the
write path needed here is small and the repo already writes deterministic JSON
artifacts through local helpers. Before implementation, confirm this custom
choice against the options above.

## Proposed Contract

Add these program-owned artifacts under each run directory:

- `execution_trace.jsonl`: append-only raw event stream, one JSON object per
  event. This is for debugging and replay.
- `execution_summary.json`: deterministic summary built from the trace and the
  plan. This is the report input of record.
- `result.json`: current `ExecutionResult`, extended with summary fields.

Extend `ExecutionResult` with:

```json
{
  "execution_trace_path": "/artifacts/<run_id>/execution_trace.jsonl",
  "execution_summary_path": "/artifacts/<run_id>/execution_summary.json",
  "step_summaries": [
    {
      "step_id": "5",
      "role": "trigger",
      "planned_action": "Click the player Stop control for ${song_title}.",
      "status": "pass | fail | skip | inconclusive",
      "decisive_attempt_id": "attempt-00021",
      "attempt_ids": ["attempt-00019", "attempt-00020", "attempt-00021"],
      "observed_outcome": "string",
      "reason": "string | null",
      "criteria_evaluation": {},
      "evidence_refs": []
    }
  ],
  "trigger_summary": {
    "step_id": "5",
    "status": "pass | fail | skip | inconclusive",
    "decisive_attempt_id": "attempt-00021",
    "reason": "string | null"
  },
  "overall_result_source": "program_derived",
  "llm_requested_overall_result": "reproduced | not_reproduced | inconclusive | null"
}
```

Keep `execution_log` as the chronological attempt log for compatibility and
debug review. New code should use `step_summaries` and `trigger_summary` when
writing reports or comparing verification runs.

## Trace Event Model

Each event should include:

- `event_id`: monotonic run-local integer or stable string.
- `timestamp`: ISO 8601 UTC timestamp.
- `run_id`.
- `event_type`: `session_started`, `action_requested`, `action_completed`,
  `step_attempt_recorded`, `capture_recorded`, `finalize_requested`,
  `summary_written`, `session_finished`, or `resource_error`.
- `request_id`: the tool request that produced the event, when applicable.
- `step_id`, `role`, and `action_label`, copied from the action payload or plan.
- `payload`: the relevant sanitized request/result data.
- `artifact_refs`: paths to DOM snapshots, screenshots, logs, and sidecar JSON.

The trace must be written before returning each tool response so a crash or
agent restart still leaves durable state.

## Step Summary Rules

Add a `PlanExecutionState` builder that consumes the plan plus recorded
attempts:

1. Initialize one summary row per planned reproduction step.
2. Assign every action attempt to a planned `step_id`; unknown or missing
   `step_id` attempts go into an `unassigned_attempts` list and cannot become
   decisive.
3. Store every attempt in chronological order under its step.
4. Mark an attempt decisive only when it contains criteria evaluation for that
   planned step's objective, or when it is the first terminal skip/blocker for
   that step.
5. Mark setup and verify steps `pass` only when their structured criteria pass.
   Failed exploratory attempts remain evidence, not final step state, if a later
   decisive attempt for the same step passes.
6. Mark the trigger step `pass` when its bug-symptom criteria pass, `fail` when
   the trigger was reached and the criteria prove the symptom absent, and
   `inconclusive` when the trigger never receives decisive evidence.
7. Derive top-level `overall_result` from `trigger_summary`:
   - `reproduced`: trigger summary `status` is `pass`.
   - `not_reproduced`: trigger summary `status` is `fail`.
   - `inconclusive`: trigger summary is missing, skipped, blocked, or
     inconclusive.

This requires the plan to carry structured criteria for web-client trigger and
verify steps. If the Markdown plan does not contain parseable criteria, the
runner should still record the raw trace, but the summary must become
`inconclusive` with a reason such as `missing structured trigger criteria`.

## Implementation Phases

### Phase 1: Contracts and Fixtures

- Add `schemas/execution_trace_event.json`.
- Extend `schemas/execution_result.json` with optional `execution_trace_path`,
  `execution_summary_path`, `step_summaries`, `trigger_summary`,
  `overall_result_source`, and `llm_requested_overall_result`.
- Add a small fixture derived from the shape of `debug/stage2web-test5-5` with
  repeated attempts for the same trigger step.
- Add tests that assert the fixture produces one trigger summary and a
  program-derived `overall_result`.

### Phase 2: Trace Recorder

- Add `tools/execution_trace.py` with:
  - `ExecutionTraceRecorder.append(event_type, **fields)`.
  - `ExecutionTraceRecorder.record_action_request(...)`.
  - `ExecutionTraceRecorder.record_action_result(...)`.
  - `ExecutionTraceRecorder.write_summary(plan, attempts)`.
- Use monotonic sequence numbers, not timestamps, for ordering.
- Write JSON atomically for `execution_summary.json`; append JSONL for the raw
  trace.

### Phase 3: Wire Stage 2

- In `WebClientRunner._start_web_client_session`, create the trace recorder and
  record `session_started`.
- In `_run_web_client_session_action`, record the requested action before
  browser execution and record the result immediately after `_execute_step`.
- In `_finalize_web_client_session`, compute the summary from recorded attempts.
  Treat the LLM's `overall_result` payload as `llm_requested_overall_result`
  only.
- In `execute_plan()` and `_execute_demo_plan()`, use the same summary builder
  so direct non-agent runs and web-client session runs produce the same result
  contract.
- Continue writing current sidecar request/result files for debugging.

### Phase 4: Criteria Ownership

- Update analysis output and `parse_reproduction_plan_markdown()` so web-client
  plans can carry structured `success_criteria`, `capture`, and optional
  `selector_assertions` per step.
- Make the runner evaluate those criteria through `tools.criteria`, matching
  the standard execution path.
- If an action payload includes ad hoc criteria, record it as attempt criteria,
  but do not let it replace the planned trigger criteria unless the planned step
  explicitly allows exploratory criteria.

### Phase 5: Report Generation

- Update `tools/report_writer.py`:
  - `_trigger_entry()` should use `trigger_summary` first.
  - Reproduction steps should pair planned steps with `step_summaries`, not the
    last raw entry per `step_id`.
  - Browser evidence should include a concise attempt timeline, while Analysis
    should cite the decisive attempt from the summary.
- Update `creatures/report/prompts/system.md` to tell the Report Agent that
  `execution_summary.json`, `step_summaries`, and `trigger_summary` are
  authoritative.
- Verification comparison should compare `trigger_summary` and decisive
  criteria results, not arbitrary first trigger entries from `execution_log`.

### Phase 6: Prompt Simplification

- Update `creatures/web_client/prompts/system.md` so `finalize` no longer asks
  the model to choose `overall_result`.
- Require the model to send `step_id`, `role`, and `action_label` with each
  action, because these are point-in-time labels recorded by the program.
- Tell the model that it does not need to summarize or recall the action
  history; the runner will return the computed `ExecutionResult`.
- Keep the one-action-per-response rule because current browser state is still
  required for choosing the next interaction.

## Test Plan

- Unit test `ExecutionTraceRecorder` appends valid JSONL events and writes a
  deterministic summary.
- Unit test repeated attempts for one step: two failed exploratory attempts plus
  one passing decisive attempt produce one passing step summary.
- Unit test repeated trigger attempts where no decisive criteria exists:
  `overall_result` becomes `inconclusive`.
- Unit test `finalize` with `overall_result: reproduced` when the trigger
  summary is `fail`: result remains `not_reproduced` and stores the requested
  value separately.
- Unit test report generation with multiple raw trigger entries: report uses
  `trigger_summary`.
- Run `.venv/bin/python -m unittest discover tests`.

## Rollout

1. Land schemas and summary builder behind additive fields.
2. Wire Web Client session runs and direct web-client runs to write traces.
3. Switch report generation to summaries.
4. Simplify prompts after tests prove the program-owned path works.
5. Remove any obsolete prompt language that asks the model to remember or
   restate complete execution history.

## Acceptance Criteria

- A web-client run can be finalized without a model-supplied `overall_result`.
- `result.json` always contains `overall_result_source: "program_derived"`.
- Stage 3 can produce the same report after loading only `result.json` and
  `execution_summary.json`, without reading the transcript.
- Repeated fallback attempts in one planned step do not create ambiguous trigger
  selection.
- A mismatch between model-requested and program-derived result is visible in
  artifacts but cannot change the official result.
