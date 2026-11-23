"""阶段一沙箱验证：20 条合成事件，增/查/撤回/重启/重建全部通过。"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from memlayer import Store  # noqa: E402

temporary = TemporaryDirectory(prefix="memlayer-baseline-")
ROOT = Path(temporary.name) / "store"

checks = []
def ok(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))

s = Store(ROOT)

# 1) 追加 + 幂等
r1 = s.append({"event_id": "ev1", "project": "p1", "subject": "alice", "text": "我每天早上喝美式咖啡"})
r1b = s.append({"event_id": "ev1", "project": "p1", "subject": "alice", "text": "我每天早上喝美式咖啡"})
ok("事件幂等：重复投递不新增事实", r1b["duplicate"] and r1b["fact_id"] == r1["fact_id"], r1b)

# 2) 偏好变更 = 新事实 + 关闭旧事实
r2 = s.supersede(r1["fact_id"], {"event_id": "ev2", "project": "p1", "subject": "alice", "text": "我改喝燕麦拿铁了", "at": "2026-11-06T01:00:00+00:00"})
cur = s.query("p1", "alice")
ok("偏好变更后 current 只有新事实", [f["text"] for f in cur] == ["我改喝燕麦拿铁了"], [f["text"] for f in cur])
allp1 = s.query("p1", "alice", mode="all")
old = [f for f in allp1 if f["id"] == r1["fact_id"]][0]
ok("旧事实保留在记录中且标记 superseded", old["status"] == "superseded", old["status"])

# 3) 遗忘 = 撤回
s.retract(r2["fact_id"], "用户要求忘掉")
cur = s.query("p1", "alice")
ok("撤回后 current 不返回被撤回事实", all(f["id"] != r2["fact_id"] for f in cur))
retr = s.get_fact(r2["fact_id"])
ok("撤回事实在记录中带墓碑", retr["status"] == "retracted", retr["status"])
ok("已撤回事实不从 all 模式回传", all(f["id"] != r2["fact_id"] for f in s.query("p1", "alice", mode="all")))
md = (ROOT / "memory" / "p1" / (r2["fact_id"] + ".md")).read_text(encoding="utf-8")
ok("Markdown 墓碑落盘", "[RETRACTED:" in md)

# 4) 视角区分：inferred 不确认
s.append({"event_id": "ev4", "project": "p1", "subject": "bob", "text": "Bob 喜欢周末爬山"})
s.append({"event_id": "ev5", "project": "p1", "subject": "bob_sister", "text": "Bob 的妹妹喜欢抹茶", "perspective": "relayed"})
inf = s.append({"event_id": "ev6", "project": "p1", "subject": "bob", "text": "Bob 可能喜欢露营", "perspective": "inferred"})
inf_row = [f for f in s.query("p1", "bob", mode="all") if f["id"] == inf["fact_id"]][0]
ok("推断事实默认未确认", inf_row["confirmed"] is False or inf_row["confirmed"] == 0, inf_row["confirmed"])
ok("转述事实单独 subject", any(f["subject"] == "bob_sister" for f in s.query("p1")))

# 5) 项目隔离 + 过期
for i in range(4):
    s.append({"event_id": f"p2ev{i}", "project": "p2", "subject": "alice", "text": f"p2 事实 {i}"})
past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
exp = s.append({"event_id": "ev_exp", "project": "p1", "subject": "alice",
                "text": "限时事实：今天下午三点开会", "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": past})
p2 = s.query("p2")
ok("p2 查询不串 p1", all(f["project"] == "p2" for f in p2) and len(p2) == 4, len(p2))
ok("过期事实不进 current", all(f["id"] != exp["fact_id"] for f in s.query("p1", "alice")))

# 6) 补足 20 条投递（含 1 次重复投递）
for i in range(8):
    s.append({"event_id": f"p1ev{i}", "project": "p1", "subject": "carol", "text": f"carol 事实 {i}"})
s.append({"event_id": "ev1", "project": "p1", "subject": "alice", "text": "我每天早上喝美式咖啡"})

st = s.status()
ok("事件数 18 条唯一（20 次投递含 2 次重复）", st["events"] == 18, st)
ok("事实总数 18", st["facts_total"] == 18, st)
ok("写队列无 pending 残留", st["queue_pending"] == 0, st)

# 7) 重启后持久
expected_alice = [f["text"] for f in s.query("p1", "alice")]
del s
s2 = Store(ROOT)
st2 = s2.status()
ok("重启后计数一致", st2 == {**st}, st2)
ok("重启后 current 查询一致", [f["text"] for f in s2.query("p1", "alice")] == expected_alice)

# 8) Markdown 为真相源：重建索引
md_count = len(list((ROOT / "memory").rglob("*.md")))
rb = s2.rebuild()
ok("Markdown 文件数 = 事实数", md_count == st["facts_total"], md_count)
ok("从 Markdown 重建索引后一致", s2.status()["facts_total"] == st["facts_total"], rb)

failed = [c for c in checks if not c[1]]
print()
print(f"结果: {len(checks) - len(failed)}/{len(checks)} 通过")
sys.exit(1 if failed else 0)
