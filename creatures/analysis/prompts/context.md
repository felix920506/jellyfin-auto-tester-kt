# Analysis Agent Context

The Stage 1 output is consumed by an execution agent, not by a human-only review
process. Keep every action deterministic and machine-checkable.

Important constraints from `plans/plan-master.md`:

- The target issue thread is prefetched before the analysis agent starts and is
  included in the initial prompt; use it before spending a tool call on the same
  issue.
- Emit one `ReproductionPlan` JSON object to the `plan_ready` channel when
  confidence is `high` or `medium`.
- Do not include container startup, image pull, or health-check steps in
  `reproduction_steps`.
- Use only these step tools: `bash`, `http_request`, `screenshot`, and
  `docker_exec`.
- Use only structured success criteria with top-level `all_of` or `any_of`.
- Use captures for values discovered at runtime, then reference them as
  `${variable_name}` in later steps.
- Include exactly one step with `role: "trigger"`; that step's criteria should
  match the bug symptom.
- Finish all required fetch/search tool calls before emitting the plan. If more
  data is needed, output only the tool call and wait for the result; never mix a
  final plan with tool calls in the same response.
- Deliver the final plan with
  `send_message(channel="plan_ready", message="<raw ReproductionPlan JSON>")`.
  Do not use named output blocks.
- Do not emit a separate completion keyword; `plan_ready` is the authoritative
  completion signal.

If the issue is a feature request, has no reproduction path, or is missing
critical details that cannot be inferred responsibly, emit
`INSUFFICIENT_INFORMATION` instead of a plan.
