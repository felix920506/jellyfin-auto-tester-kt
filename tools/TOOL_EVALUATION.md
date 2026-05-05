# Tool Evaluation for Ecosystem Extraction

This document evaluates the internal tools within this repository to identify components suitable for publishing as standalone packages within the KohakuTerrarium ecosystem. Extracting these tools would allow for broader use, outside contributions, and improved maintainability.

## High-Value Candidates for Extraction

### 1. GitHub Fetcher (`tools/github_fetcher.py`)
**Role:** Context Retrieval Engine for Analysis.
*   **Capabilities:** Wraps PyGithub to fetch structured payloads for Issues, PRs, and Discussions. It cleans content for LLMs (noise removal), handles comment pagination, and recursively resolves linked references.
*   **Ecosystem Value:** Essential for any agent performing bug analysis or issue triage. It provides "LLM-ready" context without the token-heavy metadata of raw API responses.
*   **Expansion Potential:** Support for GitLab/Jira, improved noise-filtering heuristics, and GraphQL-based batching.

### 2. Criteria Engine (`tools/criteria.py`)
**Role:** Declarative Verification and Assertion.
*   **Capabilities:** Provides a deterministic engine for evaluating success criteria using logic like `all_of`, `any_of`, `json_path` extraction, and regex matching.
*   **Ecosystem Value:** Establishes a standard schema for agents to verify their own work. It separates the "what to check" (the plan) from the "how to check" (the execution).
*   **Expansion Potential:** Visual diffing assertions, performance metric checks, and integration with standard JSONPath libraries.

### 3. Agent-Optimized Browser (`tools/browser.py`)
**Role:** LLM-Friendly Web Interaction.
*   **Capabilities:** Abstractions over Playwright including "DOM Summary," "Control Inventory," and "Media State." It filters the DOM into a format that fits within LLM context windows.
*   **Ecosystem Value:** Raw Playwright is designed for human-written scripts; agents need a "vision" system that identifies interactive elements and page state concisely.
*   **Expansion Potential:** HTML-to-Simplified-Markdown conversion, auto-waiting strategies for complex SPAs, and improved accessibility-tree-based navigation.

### 4. Docker Manager (`tools/docker_manager.py`)
**Role:** Ephemeral Infrastructure Management.
*   **Capabilities:** Manages the lifecycle of Docker containers, including version-specific pulls, health-check waiting, and port mapping.
*   **Ecosystem Value:** Provides a clean interface for agents to set up isolated, reproducible environments for testing or reproduction.
*   **Expansion Potential:** Podman/Kubernetes support, cloud-based remote environments, and advanced resource monitoring.

---

## Summary Table

| Tool | Core Value | Target Stage |
| :--- | :--- | :--- |
| **GitHub Fetcher** | Clean, recursive context retrieval | Stage 1 (Analysis) |
| **Criteria Engine** | Declarative verification logic | Stage 2/3 (Verification) |
| **Browser Driver** | LLM-friendly web interaction | Stage 2 (Execution) |
| **Docker Manager** | Reproducible environment setup | Infrastructure |

## Recommendation

The **GitHub Fetcher** and **Criteria Engine** are the strongest candidates for immediate extraction. They have minimal external dependencies and solve "pure" agent logic problems that are common across all KohakuTerrarium implementations.

The **Browser Driver** and **Docker Manager** are highly valuable but may require more effort to decouple from Jellyfin-specific assumptions before being published as general-purpose utilities.
