# Stage 3: Report Agent — Detailed Plan

## Responsibility

Receive the `ExecutionResult` from Stage 2, produce a clean human-readable `ReproductionReport`, then **re-trigger Stage 2 exactly once** using only the written report steps as validation. If verification passes, deliver the final report. If it fails, route to a human review queue without looping again.

---

## Creature Configuration

**File:** `creatures/report/config.yaml`

```yaml
name: "report_agent"
version: "1.0"

controller:
  model: "claude-opus-4-7"
  temperature: 0.3

system_prompt_file: "prompts/system.md"

max_iterations: 30
skill_mode: "dynamic"

tools:
  - name: "read"
    type: "builtin"
  - name: "write"
    type: "builtin"
  - name: "bash"
    type: "builtin"
  - name: "send_message"
    type: "builtin"
  - name: "report_writer"
    type: "custom"
    module: "tools/report_writer.py"

triggers:
  - type: "channel"
    channel: "execution_done"
    task: "analyze_and_report"

output:
  named_outputs:
    - name: "final_report"
      type: "channel"
      channel: "final_report"
    - name: "human_review_queue"
      type: "channel"
      channel: "human_review_queue"
    - name: "verification_request"
      type: "channel"
      channel: "verification_request"

memory:
  provider: "model2vec"

compact:
  threshold_tokens: 10000
  strategy: "summarize"

termination:
  keywords: ["REPORT_COMPLETE", "QUEUED_FOR_REVIEW"]
  max_turns: 35
```

---

## System Prompt (`creatures/report/prompts/system.md`)

```markdown
You are the Report Agent for the Jellyfin Auto-Tester. You receive an ExecutionResult
JSON and produce a polished, human-readable ReproductionReport. You then verify it
exactly once by re-running Stage 2.

## Your Inputs
An ExecutionResult JSON from the `execution_done` channel.
A `is_verification` boolean flag (true if this is the second run).

## Two-Pass Protocol

### If `is_verification = false` (first run): Analyze and Draft

**Step 1: Analyze the Execution**
Read the ExecutionResult carefully:
- Which steps passed, failed, or were skipped?
- What does `overall_result` say?
- What do the Jellyfin server logs reveal (errors, warnings, stack traces)?
- Do the HTTP responses confirm the bug behavior?
- Is there screenshot evidence?

**Step 2: Extract the Minimal Reproduction Steps**
From the passed steps, distill the *minimum* set of steps a maintainer needs to reproduce
the issue. Remove setup steps that are obvious (e.g. "start Docker") unless they have
non-obvious parameters. Write these as numbered, imperative steps with exact commands
and expected output.

**Step 3: Write the ReproductionReport**
Use `report_writer.generate()` to produce a structured Markdown report. See the
report schema below.

**Step 4: Send Verification Request**
Package the written steps back into a new ReproductionPlan (with `is_verification: true`)
and send it to the `verification_request` channel.
Await the result (Stage 2 will re-run and emit to `execution_done`).

### If `is_verification = true` (second run): Finalize

**Step 1: Compare results**
- Did the verification run reproduce the issue using only the written steps?
- Are the results consistent with the first run?

**Step 2: Decide routing**
- If verification PASSED (issue reproduced consistently):
  → Attach verification metadata to the report
  → Send final report to `final_report` channel
  → Emit REPORT_COMPLETE

- If verification FAILED (issue not reproduced with written steps only):
  → Append a "Verification Failure" section to the report
  → Include both run artifacts
  → Send to `human_review_queue` with a reason summary
  → Emit QUEUED_FOR_REVIEW

## Rules
- Never loop more than once. If verification fails, route to human review—do not retry.
- Write steps for a knowledgeable Jellyfin maintainer: skip basics, focus on the
  non-obvious parts.
- Exact commands and expected outputs are mandatory in the report steps.
- If `overall_result` is "not_reproduced", the report must say so clearly and offer
  possible reasons (version mismatch, environment-specific, etc.)
- If `overall_result` is "inconclusive", note what blocked reproduction and what
  additional info is needed.
```

---

## Report Schema (Markdown Structure)

The `report_writer` tool generates a Markdown file with this structure:

```markdown
# Reproduction Report: <issue_title>

**Issue:** <issue_url>
**Jellyfin Version:** <target_version>
**Result:** Reproduced | Not Reproduced | Inconclusive
**Verified:** Yes | No | Pending
**Run ID:** <run_id>
**Date:** <ISO 8601>

---

## Summary

<1-2 sentence plain-English summary of what was found.>

## Environment

| Field | Value |
|---|---|
| Docker Image | `jellyfin/jellyfin:<version>` |
| Host OS | `<from docker inspect>` |
| Architecture | `<from docker inspect>` |

## Reproduction Steps

> These steps were verified to reproduce the issue.

1. **Pull and start Jellyfin**
   ```bash
   docker run -d --name jf-test -p 8096:8096 jellyfin/jellyfin:<version>
   ```
   Wait for: `curl http://localhost:8096/health` returns `Healthy`

2. **<Next step>**
   ...

## Evidence

### Jellyfin Server Logs (relevant excerpt)
```
<log lines with ERROR/WARN highlighting>
```

### HTTP Responses
- `POST /Items/<id>/PlaybackInfo` → HTTP 500
  ```json
  { "error": "..." }
  ```

### Screenshots
![Step 3 failure](artifacts/<run_id>/screenshots/step_3_fail.png)

## Analysis

<What the logs and responses tell us about the root cause. Facts only, no speculation.>

## Verification

**Verification Run ID:** <run_id_2>
**Result:** Passed | Failed

<If failed: what differed between the two runs.>

## Notes for Maintainers

<Any ambiguities from the plan, environment-specific caveats, or open questions.>
```

---

## Custom Tool: `report_writer.py`

**File:** `creatures/report/tools/report_writer.py`

**Purpose:** Renders the structured Markdown report and saves it to disk.

**Interface:**

```python
# report_writer.generate(
#     execution_result: dict,
#     verification_result: dict = None,
#     artifacts_base: str = "/artifacts"
# ) -> dict
#   Generates the Markdown report and writes it to:
#     /artifacts/<run_id>/report.md
#   Returns {path: str, word_count: int}

# report_writer.build_verification_plan(
#     original_plan: dict,
#     written_steps: list[dict]
# ) -> dict
#   Constructs a new ReproductionPlan from the written steps for re-execution.
#   Sets is_verification=True and links back to the original run_id.
#   Returns a valid ReproductionPlan JSON dict.
```

**Implementation Notes:**
- Uses Python's `string.Template` or Jinja2 for the Markdown template
- Log excerpts are filtered: only lines containing `ERROR`, `WARN`, or matching `failure_indicators` are included (max 50 lines)
- Screenshot paths in the report use relative paths from the artifacts directory
- `build_verification_plan` preserves the original `docker_image` and `environment` but replaces `reproduction_steps` with the distilled written steps

---

## Agent Reasoning Flow

### First Pass (analysis + drafting)

```
Turn 1:  Receive ExecutionResult; check is_verification flag → false
Turn 2:  Read artifacts/<run_id>/jellyfin_server.log via read tool
Turn 3:  Read artifacts/<run_id>/http_log.jsonl; identify failing requests
Turn 4:  Analyze step-by-step outcomes; identify minimal reproduction steps
Turn 5:  Determine overall_result interpretation (reproduced / not / inconclusive)
Turn 6:  Call report_writer.generate(execution_result) → saves report.md
Turn 7:  Call report_writer.build_verification_plan(original_plan, written_steps)
Turn 8:  send_message(channel="verification_request", content=<verification_plan_json>)
         (Stage 2 wakes up, executes, emits to execution_done → triggers Turn 1 again)
```

### Second Pass (verification)

```
Turn 1:  Receive ExecutionResult; check is_verification flag → true
Turn 2:  Compare verification result to first run
Turn 3a: If consistent → report_writer.generate() with verification metadata
         send_message(channel="final_report", content=<report_path>)
         Emit REPORT_COMPLETE
Turn 3b: If inconsistent → append verification failure section
         send_message(channel="human_review_queue", content=<report + reason>)
         Emit QUEUED_FOR_REVIEW
```

---

## Output: Final Report Delivery

### On success (`final_report` channel)

The channel message contains:

```json
{
  "report_path": "/artifacts/<run_id>/report.md",
  "run_id": "<run_id>",
  "verification_run_id": "<run_id_2>",
  "overall_result": "reproduced",
  "verified": true,
  "issue_url": "https://github.com/jellyfin/jellyfin/issues/XXXX"
}
```

### On verification failure (`human_review_queue` channel)

```json
{
  "report_path": "/artifacts/<run_id>/report.md",
  "run_id": "<run_id>",
  "verification_run_id": "<run_id_2>",
  "overall_result": "reproduced",
  "verified": false,
  "reason": "Written steps failed to reproduce on second run: step 3 exit_code=1",
  "issue_url": "https://github.com/jellyfin/jellyfin/issues/XXXX"
}
```

---

## State Tracking: Distinguishing First vs. Second Pass

The Report Agent needs to know whether an incoming `execution_done` message is the first run or the verification run. This is handled by a field embedded in the `ExecutionResult`:

```json
{
  "is_verification": false,
  "original_run_id": null
}
```

The `build_verification_plan()` tool sets `is_verification: true` and `original_run_id: <first_run_id>` in the plan it sends to Stage 2. Stage 2 echoes these fields back in its `ExecutionResult`. The Report Agent reads `is_verification` to route its logic.

This avoids any need for external state or session-level variables.

---

## Edge Cases

| Scenario | Handling |
|---|---|
| `overall_result: "not_reproduced"` on first run | Report states "not reproduced"; verification step is still run (maybe it reproduced differently than expected) |
| `overall_result: "inconclusive"` on first run | Report notes blockers; verification plan sends the same steps; if still inconclusive → human review |
| Verification run crashes (container failure) | Treated as "failed verification" → human review queue |
| No screenshots available (Playwright missing) | Report skips screenshot section; logs a note |
| Artifacts dir missing or unreadable | Report Agent falls back to the in-memory ExecutionResult; notes missing artifacts |
| First run had 0 passing steps | Report states "could not execute"; skips verification; routes directly to human review |

---

## Environment Requirements

- File system write access to `artifacts/` directory
- `Jinja2` Python package (for report templating)
- No Docker access needed at this stage
- Access to `execution_done`, `final_report`, `human_review_queue`, and `verification_request` channels
