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
- Deliver the final plan with `[/output_plan_ready]... [output_plan_ready/]`, not
  `send_message`.
- Do not emit `REPRODUCTION_PLAN_COMPLETE` in a response that also contains a
  tool/function call.

If the issue is a feature request, has no reproduction path, or is missing
critical details that cannot be inferred responsibly, emit
`INSUFFICIENT_INFORMATION` instead of a plan.
