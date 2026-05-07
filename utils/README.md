# Utilities

`utils/` contains manual verification helpers that are not exposed as
KohakuTerrarium or agent tools.

## Browser Replay

`utils.browser_replay` re-executes accepted `web_client_session` browser actions
from a run's `browser_replay/replay_manifest.json`. It is useful when you want
to manually check that a browser workflow can be repeated without reading the
LLM transcript.

Run the generated per-run script:

```bash
.venv/bin/python artifacts/RUN_ID/browser_replay/replay_browser_session.py \
  --base-url http://localhost:8096
```

Or run the utility directly:

```bash
.venv/bin/python -m utils.browser_replay \
  artifacts/RUN_ID/browser_replay/replay_manifest.json \
  --headless true \
  --slow-mo-ms 250 \
  --stop-on-failure
```

Options:

- `--base-url`: Override the manifest's original Jellyfin URL.
- `--headless true|false`: Force headless or visible Chromium.
- `--slow-mo-ms N`: Slow each Playwright operation for visual inspection.
- `--stop-on-failure`: Stop after the first replayed action fails.

The Jellyfin server must already be running. Replay output is written under
`browser_replay/replay-runs/<timestamp>/`.
