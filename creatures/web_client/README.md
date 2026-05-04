# Stage 2: Web Client

Stage 2 peer for pure Jellyfin Web client issues and delegated browser tasks.

Full-plan mode listens on `web_client_plan_ready`, runs
`web_client_runner.execute_plan`, and sends the returned standard
`ExecutionResult` unchanged to `execution_done`.

Task mode listens on `web_client_task`, runs `web_client_runner.run_task` against
the supplied `base_url`, `run_id`, and `artifacts_root`, and sends the returned
`WebClientResult` to `web_client_done`. Task mode never starts or stops Docker.
