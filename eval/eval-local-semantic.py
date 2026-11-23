
"""Exercise local semantic recall with labeled fictional preferences and controls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import BridgeService, terms


FACTS = {
    "lighthouse": "灯塔场景的章名固定用「潮声｜」作前缀。",
    "dialogue": "霁岚与桑禾的对话稿使用特殊角色标识，每行分别以〔霁岚〕、〔桑禾〕开头。",
    "crossing": "渡河段落先写水流方向，再写摆渡人动作。",
    "archives": "整理档案先列来源编号，再提供三点摘要。",
    "beasts": "复盘兽潮的段落应先点明主角做错的决定。",
    "market": "描写夜市路口不展开买卖过程，只写脚步声。",
}
CASES = [
    ("lighthouse exact", "给这个灯塔场景拟一个章名。请只给一行成品。", "lighthouse"),
    ("lighthouse paraphrase", "替港口那座航标高塔所在的一幕取个章节名。单行即可。", "lighthouse"),
    ("dialogue exact", "霁岚和桑禾围绕一把铜钥匙各说一句，输出两行台词。", "dialogue"),
    ("crossing paraphrase", "让木船横过湍流，替这一节写开篇。", "crossing"),
    ("crossing second paraphrase", "挑一段摇橹过江的文字，交代水上有何变化。", "crossing"),
    ("beasts paraphrase", "野群扑城后，梳理主人公哪步判断失误。", "beasts"),
    ("arithmetic", "把13加29算成一个整数。只给出计算结果。", None),
    ("unrelated title with shared words", "给树洞听雨的场景拟一个章名。", None),
    ("unrelated chapter", "给煤矿罢工那一幕起个标题。", None),
    ("same location different task", "替登塔那场制订一份照明器材采购清单。", None),
    ("unrelated purchase", "删掉采购文件中的价格，给出执行顺序。", None),
    ("unrelated outdoor", "帮我列出出门露营要带的衣物。", None),
]
HARD_CASES = [
    ("crossing weak paraphrase", "帮我铺开船夫送旅人到对岸的这一幕。", "crossing"),
    ("archives weak paraphrase", "从这摞材料中提取要点，每个判断怎么追到原件？", "archives"),
    ("market weak paraphrase", "入暮之后的露天集散地，来来往往的行人，这一段如何落笔？", "market"),
]


def evaluate(model_dir: Path, holdout: bool = False) -> dict:
    cases = CASES + HARD_CASES
    holdout_count = 0
    if holdout:
        fixtures = Path(__file__).resolve().parent / "fixtures"
        for name in ("local-semantic-holdout.json", "local-semantic-fresh.json"):
            new_rows = json.loads((fixtures / name).read_text(encoding="utf-8"))
            cases += [(row["case"], row["query"], row["expected"]) for row in new_rows]
            if name == "local-semantic-holdout.json":
                holdout_count = len(new_rows)
    os.environ["MEMLAYER_SEMANTIC"] = "0"
    with TemporaryDirectory(prefix="memlayer-local-eval-") as directory:
        root = Path(directory) / "store"
        service = BridgeService(root, port=0, local_embedding=model_dir)
        try:
            assert service.semantic_enabled, service.semantic_status
            ids = {}
            for name, text in FACTS.items():
                ids[name] = service.store.append({
                    "event_id": name, "project": "work_fictional", "subject": "author-note",
                    "text": text, "source_msg": "source_" + name, "confirmed": True,
                    "tags": ["human-approved"],
                })["fact_id"]
            service.store.append({
                "event_id": "foreign", "project": "work_foreign", "subject": "author-note",
                "text": FACTS["lighthouse"], "source_msg": "foreign", "confirmed": True,
            })
            service.store.create_candidate({
                "project": "work_fictional", "subject": "author-note",
                "text": FACTS["lighthouse"], "source_event_id": "pending",
            })

            rows = []
            for label, query, expected in cases:
                start = perf_counter()
                recalled = service.recall("work_fictional", None, query + " 全书规划", semantic_query=query)
                elapsed = round((perf_counter() - start) * 1000, 1)
                assert all("human-approved" not in row["tags"] for row in recalled)
                actual = [name for row in recalled for name, fid in ids.items() if row["id"] == fid]
                overlap = bool(expected and terms(query) & terms(FACTS[expected]))
                passed = (len(recalled) == 1 and actual == [expected]) if expected else not recalled
                rows.append({"case": label, "expected": expected, "actual": actual, "passed": passed,
                             "lexical_overlap": overlap, "source": [row.get("match", "lexical") for row in recalled],
                             "ms": elapsed})
            assert service.store.retract(ids["lighthouse"], "fictional withdrawal")
            after = service.recall("work_fictional", None, CASES[1][1])
            assert not after
            assert not any(key.startswith(ids["lighthouse"] + "|") for key in service.emb_cache.data)
            reranker_status = service.local_reranker_status
        finally:
            service.close()

        restarted = BridgeService(root, port=0, local_embedding=model_dir)
        try:
            assert not restarted.recall("work_fictional", None, CASES[1][1])
        finally:
            restarted.close()
    cutoff = len(CASES) + len(HARD_CASES)
    return {"reranker": reranker_status, "contract": rows[:len(CASES)], "hard_paraphrases": rows[len(CASES):cutoff],
            "expanded_regression": rows[cutoff:cutoff+holdout_count],
            "fresh": rows[cutoff+holdout_count:], "withdrawal_survived_restart": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--require-reranker", action="store_true")
    args = parser.parse_args()
    result = evaluate(args.model_dir, args.holdout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    groups = ["contract"]
    if args.require_reranker:
        if result["reranker"] != "ready":
            raise SystemExit(1)
        groups.extend(("hard_paraphrases", "expanded_regression", "fresh"))
    if not all(row["passed"] for group in groups for row in result[group]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
