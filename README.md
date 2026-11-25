# Memlayer

**Carry approved project decisions into your next agent session.** Memlayer gives Codex and other local MCP clients project-scoped memory with a human review step. You can inspect the source of a recalled rule, replace it when you change your mind, or add an exception for a specific situation.

[Download v0.3.1 beta](https://github.com/chilayt/memlayer/releases/tag/v0.3.1) · [Connect Codex](#connect-codex) · [Evaluation and methodology](docs/EVALUATION.md) · [简体中文](README.zh-CN.md)

## Try a correction across two sessions

Say **“From now on, put the test command first in release handoffs.”** Your agent can submit that wording as a candidate. Review the text and project in your terminal, then approve it. In a fresh session, ask how to start a handoff. The memory tool can return the approved rule with its original source reference.

If you later say **“For hotfixes, lead with the rollback command,”** review it as a scoped exception. The regular rule stays in effect for other releases. If a rule changes while you are reviewing it, the candidate stays pending so you can review the current version.

| Capability | How it works |
| --- | --- |
| **Human review** | MCP provides recall and proposals. You approve an addition, replacement, or exception in a separate terminal. |
| **Project scope and sources** | Each adapter instance binds to a project and subject. Recall includes source references. |
| **Local storage** | Markdown is the fact ledger and SQLite is its rebuildable index. Lexical search runs locally by default. A local Chinese embedding model and reranker are optional. |
| **Retraction** | The retraction ledger and its separate mirror keep withdrawn facts out of future recall after replay. [Storage and trust boundaries](docs/SAFETY-BOUNDARIES.md) |

## Verify it before connecting your agent

With Python 3.11+ and a copy of the [v0.3.1 source release](https://github.com/chilayt/memlayer/releases/tag/v0.3.1), run the protocol smoke test from the source root. It uses an isolated fixture store and requires no model API key.

```bash
python -m pip install -r requirements-mcp.txt
python tests/mcp-stdio-check.py
```

## Connect Codex

From the same v0.3.1 source directory, start the local bridge and keep it running.

```bash
python memlayer/bridge.py --root ./data/mcp-demo --port 8770
```

Install the MCP dependency as shown above, then add this entry to your Codex `config.toml`. Replace both paths with absolute paths. Use the Python interpreter where you installed the dependency.

```toml
[mcp_servers.memlayer]
command = "/absolute/path/to/python"
args = [
  "/absolute/path/to/memlayer/mcp_stdio.py",
  "--bridge", "http://127.0.0.1:8770",
  "--project", "p_demo",
  "serve",
]
```

Run `codex mcp list` to confirm the entry. Ask the agent to call `propose_memory` for the handoff example. In your own terminal, run `pending`, then `approve` with the candidate ID it returned.

```bash
python memlayer/mcp_stdio.py --bridge http://127.0.0.1:8770 --project p_demo pending
python memlayer/mcp_stdio.py --bridge http://127.0.0.1:8770 --project p_demo approve c_0123456789abcdef
```

Use your actual candidate ID, review the displayed text and scope, and enter the confirmation shown in the terminal. Open a new session and ask your agent to call `recall_memory` about the handoff rule.

[Official OpenAI MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [OpenCode, Claude Code, generic stdio and advanced approval examples](docs/MCP.md) · [DSH adapter](plugin/README.md)

## What has been tested

| Test | Observed result |
| --- | --- |
| **v0.3.1 release regressions** | 134 engine checks, 16 DSH adapter tests, and the stdio MCP smoke test passed. The smoke test covers scope isolation, repeat submissions, reviewed additions, replacements and exceptions, and stale review targets. |
| **Real-agent workflow** | Codex CLI skipped pending text during recall, then returned the approved text and source ID in a fresh session. On v0.3.1, CodeBuddy HY4 Flash proposed a lasting correction while a one-off request stayed in the session. Both used isolated synthetic data and simulated terminal approval. |
| **Development snapshot, 108 cross-session stages** | With the same DeepSeek V4.1 Flash model over nine paired workflows, Memlayer passed 7/9 initial blind reviews and Memorix 1.10.4 passed 5/9. After recovery and rework, both finished at 9/9. The development snapshot includes later retrieval changes. |

The [evaluation report](docs/EVALUATION.md) gives the exact scope, paired timings, recovery policy, and additional retrieval and desktop tests. Memlayer is a single-user local beta. Personal adaptation still needs sustained use.

[Design](docs/DESIGN.md) · [HTTP API](docs/API.md) · [Changelog](CHANGELOG.md) · [Research notes](https://github.com/chilayt/memlayer-notes) · [Apache-2.0 license](LICENSE)
