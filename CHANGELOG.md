# Changelog

## 0.3.0 beta

- Add a local stdio MCP adapter for searching current, approved memories and submitting pending proposals.
- Pin each adapter to an operator-selected project and subject. Confirmation runs in a separate interactive terminal.
- Document Codex CLI, OpenCode v2, Claude Code, and generic stdio setup, and report the Codex CLI before-and-after approval test and OpenCode connection check.

## 0.2.4 beta

- Require local Host headers and the JSON media type for bridge writes. Configure external embedding credentials explicitly.
- Update the opt-in DSH adapter to 0.2.0-beta.2 with exact project scoping and a loopback-only bridge endpoint. Verify shadow capture, human approval, limited exceptions, and retraction against the Python bridge.
- Run the synthetic regression suite from temporary stores.

## Recent development versions

- 0.2.3 added limited exceptions to an approved preference while preserving the original wording, revision history, and both sources.
- 0.2.2 added explicit candidate replacement with approval and restart recovery.
- 0.2.1 integrated optional local semantic retrieval and reranking.

The release includes the full Apache-2.0 license and contributor notice.
