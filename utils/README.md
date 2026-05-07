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

For example, this replays the browser actions captured in
`debug/stage2web-test5-7` against the original Jellyfin demo server:

```bash
.venv/bin/python \
  debug/stage2web-test5-7/web-client-60400220-d40a-4af9-91fd-3b88f909d4cf/browser_replay/replay_browser_session.py \
  --base-url https://demo.jellyfin.org/stable \
  --headless true \
  --stop-on-failure
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

To inspect the original visual trace for the same example run:

```bash
.venv/bin/python -m playwright show-trace \
  debug/stage2web-test5-7/web-client-60400220-d40a-4af9-91fd-3b88f909d4cf/browser_replay/original_trace.zip
```

## Transcript Viewer

`utils/transcript-viewer/` is a standalone browser app for inspecting pipeline
transcripts without running a local server.

Open `utils/transcript-viewer/index.html` in a browser, then choose the project
`debug/` directory in the file picker. The viewer lists all `transcript.json`
files it finds and loads adjacent `transcript_metadata.json` files when present.
