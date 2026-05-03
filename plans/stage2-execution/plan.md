# Stage 2: Execution Agent — Detailed Plan

## Responsibility

Receive a `ReproductionPlan`, stand up the specified Jellyfin Docker container, execute every reproduction step, capture evidence (logs, screenshots, HTTP responses), and emit a detailed `ExecutionResult` that Stage 3 can analyze and report on.

---

## Creature Configuration

**File:** `creatures/execution/config.yaml`

```yaml
name: "execution_agent"
version: "1.0"

controller:
  model: "claude-opus-4-7"
  temperature: 0.1

system_prompt_file: "prompts/system.md"

max_iterations: 60       # baseline; raised dynamically — see below
skill_mode: "dynamic"

# Turn-budget scaling: a plan with N steps needs roughly
# (setup_overhead=10) + (per_step_turns=4 * N) + (assessment_overhead=6) turns
# in the worst case (dispatch + capture + criteria + log/screenshot on fail).
# main.py overrides max_iterations and termination.max_turns at trigger time:
#   max_iterations = max(60, 16 + 4 * len(plan.reproduction_steps))
#   max_turns      = max_iterations + 10
# 60 is the floor for short plans; the scaling kicks in once N > ~11.

tools:
  - name: "bash"
    type: "builtin"
  - name: "read"
    type: "builtin"
  - name: "write"
    type: "builtin"
  - name: "docker_manager"
    type: "custom"
    module: "tools/docker_manager.py"
  - name: "jellyfin_api"
    type: "custom"
    module: "tools/jellyfin_api.py"
  - name: "screenshot"
    type: "custom"
    module: "tools/screenshot.py"
  - name: "send_message"
    type: "builtin"

triggers:
  - type: "channel"
    channel: "plan_ready"
    task: "execute_plan"
  - type: "channel"
    channel: "verification_request"
    task: "execute_plan"

memory:
  provider: "model2vec"

compact:
  threshold_tokens: 12000
  strategy: "summarize"
  pin:
    # Raw Jellyfin server logs and the structured execution_log MUST survive
    # compaction verbatim — Stage 3 needs exact stack traces and outcome rows
    # to write the report. Summarization would silently drop them.
    - "jellyfin_logs"
    - "execution_log"

termination:
  keywords: ["EXECUTION_COMPLETE"]
  max_turns: 70
```

---

## System Prompt (`creatures/execution/prompts/system.md`)

```markdown
You are the Execution Agent for the Jellyfin Auto-Tester. You receive a ReproductionPlan
JSON and execute it step by step inside a Docker container, capturing all evidence.

## Your Inputs
A ReproductionPlan JSON (from the `plan_ready` or `verification_request` channel).
A `run_id` (UUID) for artifact namespacing.

## Execution Protocol

### Phase 1: Setup
1. Create artifacts directory: `/artifacts/<run_id>/`
2. Pull the Docker image specified in the plan (with progress logging)
3. Prepare all prerequisites:
   - Resolve the prerequisite cache directory:
     `prereq_dir = /artifacts/<original_run_id or run_id>/media/`
     (verification runs reuse the first-run directory so generation is not
     repeated and inputs are byte-identical to what produced the original
     result.)
   - For each prerequisite: if the target file already exists in `prereq_dir`,
     skip generation; otherwise generate or download into `prereq_dir`.
   - Create any host volume directories referenced by `plan.environment.volumes`.
   - Mount `prereq_dir` into the container at the path the steps expect
     (default `/media`).
4. Start the Jellyfin container using `docker_manager.start()`
5. Wait for Jellyfin to become healthy via `jellyfin_api.wait_healthy()` (60s).
   The probe tries `GET /health` first (modern builds, ≥10.8) and falls back
   to `GET /System/Info/Public` (older builds and forks that don't expose
   `/health`). Either a 200 from `/health` with body containing `"Healthy"`
   or any 200 from `/System/Info/Public` counts as healthy.
6. Complete the first-run StartupWizard so an admin account exists:
   - `POST /Startup/Configuration` with `{ "UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en" }`
   - `POST /Startup/User` with `{ "Name": "admin", "Password": "admin" }`
   - `POST /Startup/RemoteAccess` with `{ "EnableRemoteAccess": true, "EnableAutomaticPortMapping": false }`
   - `POST /Startup/Complete` (no body)
   This sequence is unconditional. If the wizard endpoints return 403/404 the server has already been provisioned (e.g. mounted config volume), so skip silently and proceed.
7. Authenticate as the admin user with `jellyfin_api.authenticate()`. This is
   unconditional after startup provisioning because most Jellyfin API endpoints
   require a token and `ReproductionPlan` steps do not carry separate auth
   metadata. If authentication fails, mark setup failed and emit
   `overall_result: "inconclusive"` because subsequent API steps cannot be
   evaluated reliably.

### Phase 2: Step Execution
For each step in `reproduction_steps`, in order:
1. Log the step start time; record `step.role` in the execution log entry
2. Resolve `${var_name}` references in `step.input` (and in any
   string-valued criterion fields) against the per-run variable scope
   populated by previous steps' `capture` blocks. Unresolved references
   short-circuit the step to `fail` with reason `"unbound variable: <name>"`.
3. Dispatch to the appropriate tool based on `step.tool`:
   - `bash` → run command on host or via `docker exec`
   - `http_request` → call `jellyfin_api.request()`
   - `screenshot` → call `screenshot.capture()`
   - `docker_exec` → run command inside container via `docker_manager.exec()`
4. Capture stdout, stderr, exit code, and HTTP response body/status
5. Evaluate `step.success_criteria` deterministically using the structured
   assertion DSL defined in plan-master.md (status_code, body_contains,
   body_matches, body_json_path, exit_code, stdout_contains, stderr_contains,
   log_matches, screenshot_present, combined via `all_of`/`any_of`).
   This evaluation is performed by a pure function (`evaluate_criteria(criteria, context)`),
   never by the agent's reasoning loop:
   - If criteria evaluate to true → mark step `pass`
   - If criteria evaluate to false → mark step `fail`; continue to next step (do not abort)
   - If a criterion references a tool channel that did not run for this step
     (e.g. `status_code` on a bash step) → mark step `fail` with reason
     "criterion not applicable to step.tool"
   - Store the full criteria result in `execution_log[].criteria_evaluation`,
     including each assertion's type, pass/fail result, actual value, expected
     value, and diagnostic message. Stage 3 must not recompute criteria from
     sidecar logs.
6. If the step passed and declares a `capture` block, evaluate each entry
   against the step result and bind the resulting values into the per-run
   variable scope. Capture-extraction failures (JSONPath miss, regex no-match)
   downgrade the step to `fail` with reason `"capture failed: <var>"`.
7. After any `fail` step, immediately capture:
   - Full Jellyfin server logs: `docker logs <container_id>`
   - A screenshot of current state (if UI is involved)
8. Log step end time

### Phase 3: Assessment
After all steps:
1. Retrieve full Jellyfin logs
2. Find the step with `role: "trigger"` in the execution log
3. Assess `overall_result` based solely on the trigger step's outcome:
   - `reproduced`: trigger step has `outcome: "pass"` — meaning its `success_criteria` was
     met, i.e. the bug symptom was observed
   - `not_reproduced`: trigger step has `outcome: "fail"` — meaning the bug symptom was not
     observed (the action completed without exhibiting the defect)
   - `inconclusive`: trigger step was never reached (skipped or container crashed before it),
     no step has `role: "trigger"`, or the trigger step timed out

Note: for `trigger` steps, `success_criteria` deliberately describes observing the bug
symptom (e.g. "response contains 'Transcoding failed'"). A `pass` on a trigger step means
the bug appeared as expected. A `fail` means it did not appear.

Do not use log-scanning heuristics or pass-rate counts to determine `overall_result`.
All log data is captured in `jellyfin_logs` and `execution_log` for Stage 3 to interpret.

### Phase 4: Teardown
1. Stop and remove the container
2. Preserve artifacts directory

### Phase 5: Emit Result
Build the ExecutionResult JSON. Always include:
- `run_id`: the uuid4 generated at the start of this run
- `is_verification`: copied verbatim from `plan.is_verification` (default `false` if absent)
- `original_run_id`: copied verbatim from `plan.original_run_id` (default `null` if absent)
- `execution_log[]`: one entry per planned step, including `role`, `tool`,
  stdout/stderr/exit code, HTTP response details when applicable, screenshot
  path, outcome, failure reason, duration, and criteria evaluation details

Send to the `execution_done` channel. Emit EXECUTION_COMPLETE.

## Rules
- Never modify the ReproductionPlan steps. Execute them exactly as written.
- Stage 2 exclusively owns container lifecycle. If a step's `input.command` contains
  `docker run`, `docker pull`, or `docker start`, skip it with a warning logged to
  `docker_ops.log` and mark it `skip`. Container setup has already been done in Phase 1.
- Enforce a per-step timeout of 120 seconds. Steps exceeding this are marked `fail` with
  reason "timeout".
- If the container exits unexpectedly, mark all remaining steps `skip` and set
  `overall_result: "inconclusive"`.
- All file paths in the output must be absolute.
- Do not interpret results—report facts only. Interpretation is Stage 3's job.
```

---

## Custom Tools

### `docker_manager.py`

**File:** `creatures/execution/tools/docker_manager.py`

**Purpose:** Safe wrapper around Docker SDK operations. Prevents the agent from running arbitrary `docker` shell commands.

**Interface:**

```python
# docker_manager.pull(image: str) -> dict
#   Pulls image, returns {image, digest, size_mb}

# docker_manager.start(image: str, ports: dict, volumes: list, env_vars: dict, run_id: str) -> dict
#   Starts container with name = f"jf-test-{run_id[:8]}" so concurrent runs
#   never collide on the Docker container-name namespace.
#   Returns {container_id, name, status}

# docker_manager.exec(container_id: str, command: str, timeout_s: int = 120) -> dict
#   Runs command inside container, returns {stdout, stderr, exit_code, duration_ms}

# docker_manager.logs(container_id: str, tail: int = 500) -> dict
#   Returns {logs: str} of last N lines

# docker_manager.stop(container_id: str) -> dict
#   Stops and removes container, returns {status}

# docker_manager.inspect(container_id: str) -> dict
#   Returns full docker inspect output
```

**Implementation Notes:**
- Uses the `docker` Python SDK (`docker` package), not subprocess
- Enforces image whitelist: only `jellyfin/jellyfin:*` and `jellyfin/jellyfin-web:*`
- `start()` always adds `--restart no` to prevent auto-restart pollution
- `start()` always tags containers with label `jf-auto-tester=1` so the reaper
  can identify them safely without scanning every container on the host
- Hard limit: max 2 containers running at once (prevent runaway)
- All operations logged to `artifacts/<run_id>/docker_ops.log`

**Crash-safety / leak prevention:**

- On module import, `docker_manager` runs a reaper that lists all containers
  with label `jf-auto-tester=1` whose age exceeds the max-total-run-time
  budget (30 min) and force-removes them. This handles containers orphaned
  by a previous agent crash before any new run starts.
- Every `start()` call registers an `atexit` hook (and a `SIGTERM`/`SIGINT`
  handler) that force-removes the container it created. Hooks are unregistered
  by `stop()` so the normal-path teardown remains the single source of truth.
- If the agent process is `SIGKILL`-ed the next run's import-time reaper picks
  up the leak; this is the backstop, not the primary mechanism.

---

### `jellyfin_api.py`

**File:** `creatures/execution/tools/jellyfin_api.py`

**Purpose:** Typed HTTP client for the Jellyfin REST API with session management.

**Interface:**

```python
# jellyfin_api.request(method: str, path: str, body: dict = None,
#                      headers: dict = None) -> dict
#   Makes HTTP request to http://localhost:8096{path}
#   Returns {status_code, body, headers, duration_ms}
#   Status-code expectations belong in step.success_criteria
#   (status_code assertion), evaluated by the criteria DSL — not here.

# jellyfin_api.wait_healthy(timeout_s: int = 60) -> dict
#   Polls /health (preferred); on 404 falls back to /System/Info/Public.
#   Returns {healthy: bool, elapsed_s: float, endpoint_used: str}

# jellyfin_api.authenticate(username: str = "admin", password: str = "admin") -> dict
#   Posts to /Users/AuthenticateByName
#   Returns {token, user_id, success: bool}
#   Stores token for subsequent requests

# jellyfin_api.complete_startup_wizard(admin_user: str = "admin",
#                                      admin_password: str = "admin") -> dict
#   Drives /Startup/Configuration, /Startup/User, /Startup/RemoteAccess, /Startup/Complete.
#   Idempotent: returns {already_provisioned: True} if endpoints reject as already-completed.
#   Returns {provisioned: bool, already_provisioned: bool, elapsed_s: float}
```

**Implementation Notes:**
- Uses `httpx` with a 30s connect timeout, 120s read timeout
- Auto-injects `X-Emby-Token` header if authenticated
- Retries on `ConnectionRefusedError` up to 5× with 2s backoff (server startup)
- Logs all requests/responses to `artifacts/<run_id>/http_log.jsonl`

---

### `screenshot.py`

**File:** `creatures/execution/tools/screenshot.py`

**Purpose:** Headless browser screenshot of the Jellyfin web UI at a given URL or state.

**Interface:**

```python
# screenshot.capture(url: str, run_id: str, label: str,
#                    wait_selector: str = None, wait_ms: int = 2000) -> dict
#   Takes screenshot of URL in headless Chromium
#   Returns {path: str, url: str, label: str, timestamp: str}
```

**Implementation Notes:**
- Uses `playwright` with `chromium` in headless mode
- Screenshots saved to `artifacts/<run_id>/screenshots/<label>.png`
- `wait_selector` allows waiting for a CSS selector before capture (for async UI)
- Falls back gracefully: if Playwright unavailable, returns `{path: null, error: "playwright not available"}`
- This tool is optional—steps that don't involve the web UI don't call it

---

## Agent Reasoning Flow (Turn-by-Turn)

```
Turn 1:  Receive ReproductionPlan from channel; parse JSON; generate run_id (uuid4)
Turn 2:  Create artifacts dir; docker_manager.pull(plan.docker_image)
Turn 3:  Prepare prerequisites (generate media files via bash/ffmpeg if needed)
Turn 4:  docker_manager.start(...); jellyfin_api.wait_healthy()
Turn 5:  jellyfin_api.complete_startup_wizard(); jellyfin_api.authenticate()
Turn 6-N: For each step: dispatch tool → evaluate criteria → log result
         (screenshot on fail steps if UI-related)
Turn N+1: docker_manager.logs(); find trigger step in execution_log; assess overall_result
          from trigger step outcome (pass→reproduced, fail→not_reproduced, not reached→inconclusive)
Turn N+2: docker_manager.stop()
Turn N+3: Build ExecutionResult JSON; [/send_message] @@channel=execution_done <execution_result_json> [send_message/]
Turn N+4: Emit EXECUTION_COMPLETE
```

---

## Artifact Structure

```
artifacts/<run_id>/
├── plan.json                    # Input ReproductionPlan (verbatim copy)
├── result.json                  # ExecutionResult (output)
├── docker_ops.log               # All Docker API calls and responses
├── http_log.jsonl               # All HTTP requests/responses (newline-delimited JSON)
├── jellyfin_server.log          # Full container logs at teardown
├── screenshots/
│   ├── step_3_fail.png
│   └── step_5_pass.png
└── media/
    └── test.mkv                 # Any generated/downloaded prerequisite files
```

---

## Execution Constraints & Timeouts

| Constraint | Value |
|---|---|
| Per-step timeout | 120 seconds |
| Container startup timeout | 60 seconds (health poll) |
| Docker pull timeout | 300 seconds |
| Max concurrent containers | 2 |
| Max total run time | 30 minutes |
| HTTP request timeout | 120 seconds read, 30s connect |

---

## Edge Cases

| Scenario | Handling |
|---|---|
| Docker pull fails (image not found) | Emit `ExecutionResult` with `overall_result: "inconclusive"`, error: "image not found" |
| Container exits before all steps | Mark remaining steps `skip`; collect available logs; set `inconclusive` |
| Step timeout | Mark step `fail` with `"timeout"` reason; continue |
| Port 8096 already in use | `docker_manager.start()` tries ports 8097, 8098; updates `jellyfin_api` base URL |
| Container name collision (concurrent runs) | Names are derived from `run_id` (`jf-test-<run_id[:8]>`), so collisions are statistically excluded; if a stale container with the same name exists, `start()` removes it first |
| No Playwright available | Steps with `tool: "screenshot"` log warning, save `null` path, continue |
| Media generation fails (no ffmpeg) | Mark prerequisite as failed in result; continue if step is still attempted |

---

## Environment Requirements

- Docker Engine running and accessible
- `docker` Python SDK (`pip install docker`)
- `httpx` Python package
- `playwright` + `chromium` (optional, for screenshot steps)
- `ffmpeg` CLI (optional, for media generation prerequisites)
- No GPU required; hardware transcoding steps are out of scope for initial version
