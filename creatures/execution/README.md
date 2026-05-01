# Stage 2: Execution

Owns Docker setup, deterministic step execution, evidence capture, and `ExecutionResult` emission.

See `plans/stage2-execution/plan.md` for the detailed implementation plan.

Screenshot captures run Chromium with a visible GUI by default when the host
appears to have a display. Leave `JF_AUTO_TESTER_BROWSER_HEADLESS` unset, or set
it to `auto`, for this default. Set it to `true` to force headless mode, or
`false` to force a visible browser.
