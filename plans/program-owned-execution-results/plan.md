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

Decision for this repository: use an in-repo implementation over the standard
library `json` module. The data is not generic application telemetry; it is
domain state with explicit invariants, schemas, artifact paths, and
step/attempt relationships. The write path needed here is small and the repo
already writes deterministic JSON artifacts through local helpers. No new
dependency is introduced.

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
  "overall_result_source": "program_derived | program_derived_missing_criteria | legacy_llm",
  "llm_requested_overall_result": "reproduced | not_reproduced | inconclusive | null"
}
```

Keep `execution_log` as the chronological attempt log for compatibility and
debug review. New code should use `step_summaries` and `trigger_summary` when
writing reports or comparing verification runs.

## Trace Event Model

Each event should include:

- `event_id`: monotonic run-local integer (always int; never a string).
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
4. Mark an attempt decisive using this deterministic precedence:
   1. The **last** attempt for the step that has a complete criteria evaluation
      (i.e. `criteria_evaluation.passed` is a boolean, not null/missing) wins.
      Ties — same sequence number is impossible by construction; otherwise pick
      the higher `event_id`.
   2. If no attempt has a complete criteria evaluation, the **first** terminal
      skip/blocker attempt for the step is decisive (status `skip`).
   3. Otherwise the step is `inconclusive` and `decisive_attempt_id` is null.

   Partial criteria evaluations (e.g. `any_of` aborted by a capture failure) do
   not count as complete and fall through to rule 4.2.
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

### Trust boundary for model-supplied step labels

`step_id`, `role`, and `action_label` arrive on the action payload from the
LLM. The runner cannot fully own partitioning when the partition key is
model-asserted. We accept this explicitly:

- The runner records each attempt under the model-supplied `step_id` without
  re-deriving it.
- Each `step_summary` carries `step_label_source: "model"` so consumers know
  the assignment is asserted, not verified.
- An attempt whose `step_id` does not match any planned step goes into
  `unassigned_attempts` and can never become decisive.
- Cross-checking the action payload against `planned_action` is out of scope
  for this change and tracked as a follow-up.

## Implementation Phases

### Phase 1: Contracts and Fixtures

- Add `schemas/execution_trace_event.json`.
- Extend `schemas/execution_result.json` with optional `execution_trace_path`,
  `execution_summary_path`, `step_summaries`, `trigger_summary`,
  `overall_result_source`, and `llm_requested_overall_result`.
- Add a small fixture under `tests/fixtures/program_owned_results/` derived
  from the shape of `debug/stage2web-test5-5` with repeated attempts for the
  same trigger step. Trim the copy aggressively; `debug/` is mutable and not a
  stable test input.
- Add tests that assert the fixture produces one trigger summary and a
  program-derived `overall_result`.

### Phase 2: Trace Recorder

- Add `tools/execution_trace.py` with:
  - `ExecutionTraceRecorder.append(event_type, **fields)`.
  - `ExecutionTraceRecorder.record_action_request(...)`.
  - `ExecutionTraceRecorder.record_action_result(...)`.
  - `ExecutionTraceRecorder.write_summary(plan, attempts)`.
- Use monotonic sequence numbers, not timestamps, for ordering.
- Write `execution_summary.json` atomically (write to `*.tmp` then `os.replace`).
- For `execution_trace.jsonl`, serialize each event to a single bytes buffer
  ending in `\n` and append with one `os.write` call on a file opened with
  `O_APPEND`. Readers must tolerate a trailing partial line in case of crash
  during write. Optional `os.fsync` after each event for durability.

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

### Phase 4: Criteria Ownership (precedes Phase 5)

This phase must land before Phase 5 flips report generation to summaries;
otherwise every web-client run falls into the `inconclusive` branch of Rule 7
because no plan carries structured criteria.

- Update analysis output and `parse_reproduction_plan_markdown()` so web-client
  plans can carry structured `success_criteria`, `capture`, and optional
  `selector_assertions` per step.
- Make the runner evaluate those criteria through `tools.criteria` (reusing
  `evaluate_criteria`, `extract_captures`, `resolve_references`,
  `normalize_criteria_assertion` — no new evaluator), matching the standard
  execution path.
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
- Cross-version fallback: if either side of a comparison lacks
  `trigger_summary` (e.g. an older first-run loaded against a newer
  verification run), synthesize one on the fly from `execution_log` using the
  Step Summary Rules above, so comparisons stay meaningful during rollout.
- While Phase 4 is being adopted, if `trigger_summary.status` is
  `inconclusive` solely because of `missing structured trigger criteria`,
  fall back to the first trigger entry from `execution_log` for report
  rendering. Mark this on the report so it is visible.

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
- Unit test report generation builds successfully from `result.json` plus
  `execution_summary.json` alone, with the transcript file absent, and
  produces output equivalent to a run that did read the transcript. This
  exercises the third acceptance criterion directly.
- Unit test cross-version verification comparison: a first-run lacking
  `trigger_summary` is compared against a verification run that has one;
  the synthesized `trigger_summary` from `execution_log` produces a
  meaningful comparison.
- Run `.venv/bin/python -m unittest discover tests`.

## Rollout

1. Land schemas and summary builder behind additive fields (Phase 1–2).
2. Wire Web Client session runs and direct web-client runs to write traces
   (Phase 3). Verification comparison uses synthesized `trigger_summary` for
   any side that lacks one.
3. Land criteria parsing in Markdown plans and runner evaluation (Phase 4)
   before flipping report generation.
4. Switch report generation to summaries (Phase 5).
5. Simplify prompts (Phase 6) only after Phase 5 has been verified in real
   runs — removing "remember actions" before the report path uses summaries
   would leave reports relying on absent recall.
6. Remove any obsolete prompt language that asks the model to remember or
   restate complete execution history.

## Acceptance Criteria

- A web-client run can be finalized without a model-supplied `overall_result`.
- `result.json` always contains `overall_result_source` set to one of
  `program_derived`, `program_derived_missing_criteria`, or `legacy_llm`
  (the last only for runs predating this change).
- Stage 3 can produce the same report after loading only `result.json` and
  `execution_summary.json`, without reading the transcript.
- Repeated fallback attempts in one planned step do not create ambiguous trigger
  selection.
- A mismatch between model-requested and program-derived result is visible in
  artifacts but cannot change the official result.
