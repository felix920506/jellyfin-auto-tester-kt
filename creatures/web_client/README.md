# Stage 2: Web Client

Stage 2 peer for pure Jellyfin Web client issues and delegated browser tasks.

Full-plan mode listens on `web_client_plan_ready`, starts `web_client_session`
with a `plan_markdown_path`, chooses one browser action at a time from the plan and
evidence, and sends the final standard `ExecutionResult` unchanged to
`execution_done`. The tool owns one active session at a time; actions and
finalize apply to the current session without an explicit identifier.

Task mode listens on `web_client_task` and runs the interactive browser session
protocol: `start` creates the active session, each `action` message executes
exactly one Playwright action, and `finalize` closes the browser. The returned
`WebClientResult` from `web_client_session` is sent to `web_client_done`. Task
mode never starts or stops Docker.
