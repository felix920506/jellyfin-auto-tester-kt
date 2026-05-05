# Custom Implementations with Off-the-Shelf Alternatives

This document tracks custom implementations within the project where well-established, off-the-shelf alternatives are available in the Python ecosystem. Replacing these could improve maintainability, robustness, and reduce the codebase size.

## 1. JSONPath Parsing
*   **Location:** `tools/criteria.py:extract_json_path`
*   **Custom Implementation:** A manual parser that handles a subset of JSONPath (dotted keys and bracketed indexes/keys).
*   **Off-the-shelf Alternative:** `jsonpath-ng` or `jsonpath-rw`. These provide full specification support and more robust error handling.

## 2. Async/Sync Thread Management
*   **Location:** `tools/async_compat.py:run_sync_away_from_loop`
*   **Custom Implementation:** Manually manages `ThreadPoolExecutor` and `threading.local` to run synchronous code (specifically Playwright) outside the main asyncio loop.
*   **Off-the-shelf Alternative:** `asyncio.to_thread` (Python 3.9+) or `loop.run_in_executor` from the standard library.

## 3. ISO8601 Date Parsing
*   **Location:** `tools/docker_manager.py:_parse_docker_created`
*   **Custom Implementation:** Uses manual string replacement (e.g., `Z` to `+00:00`) and regex splitting to normalize Docker's timestamp format before calling `datetime.fromisoformat`.
*   **Off-the-shelf Alternative:** `python-dateutil` (`dateutil.parser.isoparse`), which handles varied ISO8601 formats natively.

## 4. HTTP Retry Logic
*   **Location:** `tools/jellyfin_http.py:_request_with_retries`
*   **Custom Implementation:** A manual `for` loop with `time.sleep` and exception checking for connection errors.
*   **Off-the-shelf Alternative:** `tenacity` for general retries, or the built-in `Transport` retry configurations in `httpx` (which is already a project dependency).

## 5. HTML Content Extraction
*   **Location:** `tools/browser.py:_summarize_html` and `_html_text`
*   **Custom Implementation:** Uses regular expressions (`<[^>]+>`) to strip HTML tags and extract text.
*   **Off-the-shelf Alternative:** `BeautifulSoup` (bs4) with `get_text()`. Regex-based HTML stripping is brittle against malformed HTML or script/style tags.

## 6. Variable Reference Resolution
*   **Location:** `tools/criteria.py:resolve_references`
*   **Custom Implementation:** A recursive function that searches through nested dicts/lists for `${var_name}` strings and replaces them using a regex sub-callback.
*   **Off-the-shelf Alternative:** `jinja2` for complex templating or `string.Template` for simpler cases.

## 7. File Path Relativity
*   **Location:** `tools/report_writer.py:_relative_artifact_path`
*   **Custom Implementation:** Manual try/except blocks around `Path.relative_to` combined with `os.path.relpath`.
*   **Off-the-shelf Alternative:** `Path.relative_to(walk_up=True)` (Python 3.12+) or consistent use of `os.path.relpath`.

## 8. Unique Filename Generation
*   **Location:** `tools/browser.py:_unique_path`
*   **Custom Implementation:** A manual loop from 2 to 1000 checking `path.exists()` to append `_index` to a filename.
*   **Off-the-shelf Alternative:** Robust logic found in libraries like `tempfile` or specialized filesystem utilities.

## 9. Map/Noise Filtering
*   **Location:** `tools/github_fetcher.py:_compact_output_value`
*   **Custom Implementation:** Recursive dictionary cleaning to remove "noise" fields and empty values.
*   **Off-the-shelf Alternative:** Pydantic models with `exclude_none=True` / `exclude_unset=True` or generic utility libraries.
