# Analysis Agent Context

The Stage 1 output is consumed by an execution agent, not by a human-only review
process. Keep every action deterministic and machine-checkable.

Important constraints from `plans/plan-master.md`:

- The target issue thread is prefetched before the analysis agent starts and is
  included in the initial prompt; use it before spending a tool call on the same
  issue.
- Emit one `ReproductionPlan` JSON object when confidence is `high` or
  `medium`: use `plan_ready` for standard execution, or
  `web_client_plan_ready` for exclusively Jellyfin Web client issues.
- Set top-level `execution_target` to `"web_client"` only for pure Jellyfin Web
  client bugs whose trigger step uses `tool: "browser"`. Use `"standard"` for
  server, API, transcoding, plugin, startup, filesystem, and mixed ownership
  bugs.
- For safe browser-only web-client bugs, you may set `server_target.mode` to
  `"demo"` and run against the public demo servers instead of Docker. Demo mode
  requires browser-only reproduction, demo media sufficiency, no admin
  privileges, no server/API mutation, no custom media, and no exact historical
  version requirement.
- Demo URL mapping is exact: stable/latest/latest-stable use
  `https://demo.jellyfin.org/stable`; unstable/latest-unstable/master use
  `https://demo.jellyfin.org/unstable`. Demo login is username `demo` with a
  blank password.
- Keep Docker/standard execution when the issue needs a specific old version,
  custom media, admin settings, plugins, logs, API calls, transcoding setup, or
  server-side assertions.
- Do not include container startup, image pull, or health-check steps in
  `reproduction_steps`.
- Use only these step tools: `bash`, `http_request`, `screenshot`,
  `docker_exec`, and `browser`.
- Prefer `browser` for pure Jellyfin Web flows involving DOM state, navigation,
  form/UI interactions, client-side media controls, browser console evidence,
  or screenshots after user interaction. Prefer `http_request` for API-level
  bugs and the standard path for mixed client/server ownership.
- Use only structured success criteria with top-level `all_of` or `any_of`.
- Use captures for values discovered at runtime, then reference them as
  `${variable_name}` in later steps.
- Include exactly one step with `role: "trigger"`; that step's criteria should
  match the bug symptom.
- Finish all required fetch/search tool calls before emitting the plan. If more
  data is needed, output only the tool call and wait for the result; never mix a
  final plan with tool calls in the same response.
- Deliver the final plan with a single `send_message` tool-call block:
  `[/send_message]`, `@@channel=plan_ready` or
  `@@channel=web_client_plan_ready`, raw JSON body, `[send_message/]`. Do not
  use named output blocks or Python-call syntax.
- Do not emit a separate completion keyword; the channel send is the
  authoritative completion signal.

If the issue is a feature request, has no reproduction path, or is missing
critical details that cannot be inferred responsibly, emit
`INSUFFICIENT_INFORMATION` instead of a plan.
