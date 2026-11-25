# Memlayer evaluation

Updated November 25, 2026. The public release is **v0.3.1 beta**. Development results use a later snapshot based on v0.3.1. The paired development study covers three synthetic workloads. Results on other workloads and long-term personal adaptation remain open questions.

## Public release verification

| Surface | What we exercised | Observed result |
| --- | --- | --- |
| **Python engine** | Event idempotency, current and historical facts, scoped reads, approval conflicts, retraction, replay, and index rebuild. | 134 checks passed on v0.3.1. |
| **DSH adapter** | Project-scoped capture and recall with the Node test suite, plus an integration test against the Python bridge. | 16 adapter tests passed. The bridge integration exercised shadow capture, an approved exception, and retraction. |
| **MCP stdio** | Discovery, project and subject isolation, validation, repeat submissions, interactive approval, replacement, exceptions, and edits or withdrawal during review. | The end-to-end smoke test passed. Stale reviews leave the candidate pending. |
| **Codex CLI 0.163.0** | Two real Codex sessions on v0.3.0 with one isolated synthetic store and simulated terminal approval. | A pending proposal returned zero matches. After approval, a new session returned the same text and original source ID. |
| **CodeBuddy HY4 Flash** | Two synthetic sessions on v0.3.1 with a lasting correction and a one-time request. The harness simulated approval of the replacement. | One candidate for the lasting correction. The old rule remained current until approval, then a fresh session recalled the replacement. |
| **OpenCode 2.2.7** | Local MCP discovery on Windows. | The client reported the server connected. |

Download the [v0.3.1 source archive](https://github.com/chilayt/memlayer/releases/tag/v0.3.1) and run these commands with Python 3.11+ from the source root. The protocol test provides a loopback bridge and an isolated fixture store. It requires no model API key.

```bash
python -m pip install -r requirements-mcp.txt
python tests/mcp-stdio-check.py
```

The archive also includes the [engine tests](https://github.com/chilayt/memlayer/tree/v0.3.1/tests) and the [DSH adapter verification commands](https://github.com/chilayt/memlayer/tree/v0.3.1/plugin). For the DSH bridge integration, set the `MEMLAYER_PYTHON` environment variable to your Python 3.11+ interpreter when it is outside your default PATH. See [MCP setup and manual approval](MCP.md) for an interactive walkthrough.

## Development snapshot with later recall fixes

### Same-model DSH comparison with Memorix

A separate task-authoring agent froze three synthetic tasks covering an engineering change, a release evidence report, and a fiction continuation. Each task ran three times against each backend. Each workflow had six required stages in fresh DSH sessions. The completed workflows account for **108 required-stage sessions**. Retry and repair attempts were tracked separately. A separate reviewer scored anonymized deliverables against the frozen requirements.

The backends were a **Memlayer development snapshot based on v0.3.1** and **Memorix 1.10.4**. Both used DSH OpenCode Go with **DeepSeek V4.1 Flash at max reasoning**, the same local embedding weights, source material and authorized memory decisions. The starting backend alternated across repeats. The workflows included mid-task corrections, withdrawal of an older instruction, and follow-up queries. One human reviewed six decisions together. Both systems reused the approved decisions and each cumulative total counts the same 152.3 seconds of human review once.

| Metric across nine paired workflows | Memlayer snapshot | Memorix 1.10.4 |
| --- | ---: | ---: |
| All six stages completed normally on the first run | 6/9 | 5/9 |
| Initial deliverables passing blind review | 7/9 | 5/9 |
| Final deliverables passing blind review after recovery or repair | 9/9 | 9/9 |
| Recovery or repair operations counted as rework | 5 | 6 |
| Cumulative workflow time including recovery, repair and one shared approval period | 17,882.0 s | 18,974.0 s |

Some runs timed out or needed repair. In the four pairs where both sides completed cleanly on the first run, both scored 4/4. Their cumulative times were 5,386.3 s and 6,365.4 s, respectively, including the shared review period. The timing sums individual task durations. Initial blind review scores the first available artifact, including artifacts from runs that later needed recovery to complete all six stages.

### Retrieval and desktop integration

- **Chinese retrieval on the development snapshot.** The same CodeBuddy HY4 Flash model received eight original prompts and six held-out prompts with three memory conditions, for 42 responses. With local embeddings and reranking, the retrieval layer hit all six relevant original prompts. Extra memory injection in the original eight fell from five prompts to zero. It hit all three held-out paraphrases and returned empty recall on all three unrelated held-out queries. In the retrieved-memory arm, all 14 fixture answers passed.
- **Local e-commerce and writing desktop hosts.** We tested approved preferences across fresh topics or conversations, scope isolation, and removal from recall after withdrawal. These tests used real desktop and bridge code with synthetic fixture-model requests. The e-commerce regression also paired a filming-opening question with an unrelated writing-opening question.

The source archive reproduces the published protocol checks. Development figures are aggregate results from the separately described local workflows.
