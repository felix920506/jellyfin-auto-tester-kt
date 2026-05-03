# Stage 1: Analysis Agent — Detailed Plan

## Responsibility

Read a Jellyfin GitHub issue, fetch all supporting context, and produce a structured `ReproductionPlan` JSON that Stage 2 can execute deterministically. Also flag low-confidence or ambiguous issues before any Docker work begins.

---

## Creature Configuration

**File:** `creatures/analysis/config.yaml`

```yaml
name: "analysis_agent"
version: "1.0"

controller:
  model: "claude-opus-4-7"
  temperature: 0.2

system_prompt_file: "prompts/system.md"

max_iterations: 20
skill_mode: "dynamic"

tools:
  - name: "web_fetch"
    type: "builtin"
  - name: "web_search"
    type: "builtin"
  - name: "read"
    type: "builtin"
  - name: "bash"
    type: "builtin"
  - name: "github_fetcher"
    type: "custom"
    module: "tools/github_fetcher.py"
  - name: "send_message"
    type: "builtin"

memory:
  provider: "model2vec"

compact:
  threshold_tokens: 8000
  strategy: "summarize"

termination:
  keywords: ["INSUFFICIENT_INFORMATION"]
  max_turns: 25
```

---

## System Prompt (`creatures/analysis/prompts/system.md`)

```markdown
You are the Analysis Agent for the Jellyfin Auto-Tester. Your job is to read a Jellyfin
GitHub issue and produce a precise, executable ReproductionPlan.

## Your Inputs
- A GitHub issue URL
- A target container version (e.g. "10.9.7") provided by the maintainer
- A prefetched GitHub issue thread JSON object for the target issue

## Your Process

### Step 1: Read the Prefetched Issue
Start from the prefetched issue thread in the initial prompt. It contains:
- Issue title, body, labels, and all comments
- Linked pull requests or referenced issues when available
- The reporter's environment details (OS, browser, Jellyfin version, client type)

Do not call `github_fetcher` for the same target issue unless the prefetched
thread is missing required fields or appears stale. Use it for linked issues or
pull requests when the included summaries are insufficient.

### Step 2: Gather Supporting Context
- If the issue references a specific media format, codec, or file type, search for known
  Jellyfin behavior or related issues using `web_search`.
- Fetch any linked external resources (logs, screenshots, config files) via `web_fetch`.
- If the issue references a Jellyfin API endpoint or feature, fetch the relevant
  Jellyfin docs page.

### Step 3: Identify Reproduction Requirements
Determine:
1. Which Jellyfin component is involved (server, web client, specific plugin, transcoding)
2. What prerequisites are needed (specific media files, library structure, user account state)
3. The sequence of actions that triggers the bug
4. What "reproduced" looks like (error message, wrong behavior, missing UI element)
5. What "not reproduced" looks like

### Step 4: Assess Confidence
Rate confidence as:
- **high**: Clear steps, clear expected outcome, no ambiguities
- **medium**: Steps are mostly clear but some details are missing or inferred
- **low**: Steps are vague, contradictory, or critically incomplete

If confidence is **low**, emit INSUFFICIENT_INFORMATION with a list of missing details.
Do not proceed to plan generation.

### Step 5: Emit the ReproductionPlan
Produce a valid JSON object conforming to the ReproductionPlan schema (see schemas/reproduction_plan.json).

**Steps begin after the container is already healthy.** Stage 2 unconditionally handles
pulling the image, starting the container, and waiting for `/health`. Never include steps
like "pull image", "docker run", "start Jellyfin", or "wait for health" in
`reproduction_steps`—they will be executed a second time and cause port conflicts or
duplicate containers.

Each step must have a `tool` field specifying how Stage 2 should execute it:
- `"bash"` — shell command on the host (e.g. file preparation, ffmpeg)
- `"http_request"` — HTTP call to the Jellyfin API or web UI
- `"screenshot"` — capture browser state at this step
- `"docker_exec"` — command inside the already-running container

When a step needs a value produced by an earlier step (e.g. an item ID returned by a
library scan), declare a `capture` block on the producing step and reference the
variable as `${name}` inside any later step's `input`. See plan-master.md for the
capture/interpolation rules. Never embed placeholder strings like `{item_id}` —
they will be sent to Jellyfin verbatim.

Send the final plan to the `plan_ready` channel with exactly one
`send_message` tool-call block:

```text
[/send_message]
@@channel=plan_ready
{ ... valid ReproductionPlan JSON ... }
[send_message/]
```

The closing tag is `[send_message/]`, not `[/send_message]`. The block body
becomes the `message` value and must be the raw `ReproductionPlan` JSON
serialized as text: no Markdown fences, no prose, no wrapper object, and no
named output block. Do not write Python-call syntax such as
`send_message(channel="plan_ready", ...)`; it will not execute.
After the `send_message` call is made, stop; the runner treats the `plan_ready`
channel message as the completion signal.

Never emit a separate completion keyword after the plan. If you need a tool
result, output only the tool call block and wait for the next turn.

## Rules
- Never invent steps the issue doesn't support. Ambiguity goes in `ambiguities`, not in steps.
- `success_criteria` MUST be a structured `{all_of|any_of: [...]}` object using
  the assertion DSL defined in plan-master.md. Never emit free-text criteria —
  Stage 2 evaluates them programmatically, not by LLM judgment, so unstructured
  criteria are unrunnable.
- Prefer `http_request` over browser automation for API-level bugs.
- Docker image must be `jellyfin/jellyfin:<version>` using the maintainer-specified version.
```

---

## Custom Tool: `github_fetcher.py`

**File:** `creatures/analysis/tools/github_fetcher.py`

**Purpose:** Wraps the GitHub APIs to retrieve structured GitHub URL data without HTML parsing.

**Interface:**

```python
# Tool name: github_fetcher
# Arguments:
#   url: str               — full github.com URL; type is inferred
#   include_comments: bool — default True
#   include_linked: bool   — default True (fetches linked PRs/issues/discussions)

# Returns: dict with keys:
#   kind, url, title/body/message/content fields as appropriate
#   comments: list of {author, body, created_at}
#   linked_issues: list of {url, title, state}
#   linked_prs: list of {url, title, state, merged}
#   linked_discussions: list of {url, title, state, category}
```

**Implementation Notes:**
- Uses `GITHUB_TOKEN` env var if present (avoids rate limits)
- Infers issue, PR, discussion, commit, code file, directory, or repository from the URL
- Retries issue/PR/discussion API shapes when a numbered URL path is wrong
- For linked items: parses `closes #N`, `fixes #N`, `#N` references in body/comments
- Returns plain dict; agent serializes to JSON in its reasoning

---

## Agent Reasoning Flow (Turn-by-Turn)

```
Turn 1:  Parse input → read prefetched issue thread JSON
Turn 2:  Read issue body → identify component, environment, steps mentioned
Turn 3:  web_fetch any linked external resources (logs, screenshots)
Turn 4:  web_search for related issues or known behavior if needed
Turn 5:  Assess confidence; if low → emit INSUFFICIENT_INFORMATION + halt
Turn 6:  Draft ReproductionPlan JSON
Turn 7:  Self-review: are all steps executable? Are success criteria objective?
Turn 8:  [/send_message] @@channel=plan_ready <plan JSON> [send_message/]; Stage 1 is complete
```

---

## Output

The agent sends a `ReproductionPlan` JSON to the `plan_ready` channel. See the master plan for the full schema.

**Example partial output:**

```json
{
  "issue_url": "https://github.com/jellyfin/jellyfin/issues/12345",
  "issue_title": "Transcoding fails for H.265 10-bit content on ARM",
  "target_version": "10.9.7",
  "docker_image": "jellyfin/jellyfin:10.9.7",
  "prerequisites": [
    {
      "type": "media_file",
      "description": "H.265 10-bit HEVC sample file",
      "source": "generate with ffmpeg: ffmpeg -f lavfi -i testsrc=size=1920x1080 -c:v libx265 -x265-params 'colorprim=bt2020' -t 10 test.mkv"
    }
  ],
  "reproduction_steps": [
    {
      "step_id": 1,
      "action": "Add H.265 10-bit test file to the media library via the API",
      "role": "setup",
      "tool": "http_request",
      "input": {
        "method": "POST",
        "path": "/Library/Media/VirtualFolders",
        "body": { "Name": "TestLib", "CollectionType": "movies", "Paths": ["/media"] }
      },
      "expected_outcome": "HTTP 204; library scan triggered",
      "success_criteria": { "all_of": [ { "type": "status_code", "equals": 204 } ] }
    },
    {
      "step_id": 2,
      "action": "Query the library and capture the generated Jellyfin item ID",
      "role": "setup",
      "tool": "http_request",
      "input": {
        "method": "GET",
        "path": "/Items?Recursive=true&IncludeItemTypes=Movie"
      },
      "expected_outcome": "HTTP 200 with the generated test media listed",
      "success_criteria": {
        "all_of": [
          { "type": "status_code", "equals": 200 },
          { "type": "body_contains", "value": "test.mkv" }
        ]
      },
      "capture": {
        "item_id": { "from": "body_json_path", "path": "$.Items[0].Id" }
      }
    },
    {
      "step_id": 3,
      "action": "Request playback info for the HEVC item to trigger transcoding decision",
      "role": "trigger",
      "tool": "http_request",
      "input": {
        "method": "POST",
        "path": "/Items/${item_id}/PlaybackInfo",
        "body": { "DeviceProfile": { "MaxStreamingBitrate": 2000000 } }
      },
      "expected_outcome": "HTTP 500 or TranscodingInfo.IsVideoDirect=false with error in logs",
      "success_criteria": {
        "any_of": [
          { "type": "body_contains", "value": "Transcoding failed" },
          { "type": "log_matches", "pattern": "HEVC decode error" }
        ]
      }
    }
  ],
  "confidence": "high",
  "ambiguities": []
}
```

---

## Edge Cases

| Scenario | Handling |
|---|---|
| Issue is a feature request, not a bug | Agent outputs `INSUFFICIENT_INFORMATION` with reason "not a bug report" |
| Issue references a private log paste | Agent notes it in `ambiguities`, proceeds with available info |
| Issue has no reproduction steps at all | Confidence → `low`, halt with `INSUFFICIENT_INFORMATION` |
| GitHub rate limit hit without token | Agent falls back to `web_fetch` on the raw GitHub URL |
| Issue is already closed/fixed | Agent notes in plan metadata; Stage 2 still attempts reproduction |

---

## Environment Requirements

- `GITHUB_TOKEN` (optional, recommended) — GitHub Personal Access Token for higher rate limits
- Network access to `api.github.com` and any linked external URLs
- No Docker access needed at this stage
