"""Semantic candidates: zero-lexical-overlap facts can only surface in all-mode."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import BridgeService  # noqa: E402

checks = []


def ok(name, condition, detail=""):
    checks.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))


class StubSemantic:
    """Embeds by topic word so a synonym query maps to the fact's vector."""
    model = "stub"
    dim = 2

    def embed(self, texts):
        out = []
        for text in texts:
            out.append([1.0, 0.0] if ("凭据" in text or "password" in text) else [0.0, 1.0])
        return out


def run(mode):
    temp = Path(tempfile.mkdtemp(prefix="semantic-candidates-"))
    os.environ["MEMLAYER_SEMANTIC"] = "1"
    if mode:
        os.environ["MEMLAYER_SEMANTIC_CANDIDATES"] = mode
    else:
        os.environ.pop("MEMLAYER_SEMANTIC_CANDIDATES", None)
    service = BridgeService(temp / "store", port=0)
    service.semantic_enabled = True
    service.semantic_mode = "active"
    service.semantic = StubSemantic()
    import semantic as real_semantic
    service._sem = real_semantic  # module: cosine/rrf helpers live here
    import threading
    service._emb_lock = threading.Lock()
    service._emb_pending_path = temp / "store" / "emb-cleanup-pending.json"
    service.emb_cache = real_semantic.EmbedCache(temp / "store" / "emb-cache.json", "stub", 2)
    service.store.on_retract(service._emb_on_retract)
    target = service.store.append({"event_id": "e1", "project": "p_sem", "subject": "u",
                                   "text": "凭据收在铁柜中", "source_msg": "s1",
                                   "confirmed": True})["fact_id"]
    other = service.store.append({"event_id": "e2", "project": "p_sem", "subject": "u",
                                  "text": "今日天气晴", "source_msg": "s2",
                                  "confirmed": True})["fact_id"]
    try:
        facts = service.recall("p_sem", None, "password 放哪儿", 5)
        return {f["id"] for f in facts}, target, other
    finally:
        service.store.close()


lex_ids, target, other = run(None)
ok("默认词法候选：零重叠同义事实无法被发现", target not in lex_ids, sorted(lex_ids))

all_ids, target2, other2 = run("all")
ok("all 模式：零词法重叠的同义事实可被语义命中", target2 in all_ids, sorted(all_ids))
ok("all 模式：无关事实不被硬塞", other2 not in all_ids, sorted(all_ids))

print(f"\n结果: {sum(checks)}/{len(checks)} 通过")
sys.exit(0 if all(checks) else 1)
