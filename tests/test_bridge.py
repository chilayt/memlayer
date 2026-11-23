"""End-to-end checks for the loopback bridge and crash recovery."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import BridgeService  # noqa: E402
from memlayer import Store, now_iso  # noqa: E402

checks = []


def ok(name, condition, detail=""):
    checks.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))


def request(url, method="GET", payload=None, headers=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def event(eid, text, **extra):
    return {"event_id": eid, "project": "p1", "subject": "alice", "text": text,
            "trust": "direct", "confirmed": True, **extra}


def main():
    temp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
    service = BridgeService(temp / "http", port=0).start()
    url = service.url
    try:
        status, health = request(url + "/health")
        ok("health endpoint", status == 200 and health["ok"] and health["version"])
        blocked_hosts = request(url + "/health", headers={"Host": "example.invalid"})
        blocked_page = request(url + "/v1/candidates", "POST", {}, {"Origin": "https://example.invalid"})
        blocked_type = request(url + "/v1/candidates", "POST", {}, {"Content-Type": "application/json-extra"})
        ok("本机桥拒绝非本机 Host、网页 Origin 及非 JSON 媒体类型",
           [blocked_hosts[0], blocked_page[0], blocked_type[0]] == [403, 403, 400])
        ok("默认撤回镜像位于 root 之外", service.store.mirror is not None
           and service.root.resolve() not in service.store.mirror.parents)

        def post(i):
            return request(url + "/v1/events", "POST", event(f"http-{i}", f"第 {i} 条事实 K{i:03d}"))

        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(post, range(24)))
        ok("24 个并发写入全部成功", all(s == 200 and b["ok"] for s, b in responses))
        first_id = responses[0][1]["result"]["fact_id"]
        status, dup = request(url + "/v1/events", "POST", event("http-0", "重复投递不应新增"))
        ok("HTTP 重复投递幂等", dup["result"]["duplicate"] and dup["result"]["fact_id"] == first_id)
        _, state = request(url + "/v1/status")
        ok("单写者并发后计数正确", state["status"]["events"] == 24 and
           state["status"]["facts_total"] == 24 and state["status"]["queue_pending"] == 0,
           state["status"])

        status, recalled = request(url + "/v1/facts?project=p1&subject=alice&q=K003&limit=3")
        ok("只读召回到目标事实", status == 200 and any("K003" in f["text"] for f in recalled["facts"]),
           [f["text"] for f in recalled.get("facts", [])])

        status, sup = request(url + "/v1/facts/supersede", "POST", {
            "old_fact_id": first_id,
            "event": event("http-0-rev", "第 0 条改为 NEW-VALUE")})
        ok("HTTP 偏好变更", status == 200 and sup["ok"])
        new_id = sup["result"]["fact_id"]
        _, current = request(url + "/v1/facts?project=p1&subject=alice&q=NEW-VALUE")
        _, old = request(url + "/v1/facts?project=p1&subject=alice&q=K000&mode=all")
        ok("变更后新事实可召回", any(f["id"] == new_id for f in current["facts"]))
        old_item = next(f for f in old["facts"] if f["id"] == first_id)
        ok("旧事实已关闭", old_item["status"] == "superseded" and old_item["valid_to"])

        # Compound question: every part should surface its own fact.
        _, compound = request(url + "/v1/facts?project=p1&subject=alice&q="
                              + urllib.parse.quote("K003 是什么？K017 是什么？") + "&limit=5")
        texts = [f["text"] for f in compound["facts"]]
        ok("复合问题分段召回到两部分事实",
           any("K003" in t for t in texts) and any("K017" in t for t in texts), texts)

        status, ret = request(url + "/v1/facts/retract", "POST",
                              {"fact_id": new_id, "reason": "HTTP 撤回测试"})
        ok("HTTP 撤回", status == 200 and ret["result"]["retracted"])
        _, after = request(url + "/v1/facts?project=p1&subject=alice&q=NEW-VALUE")
        ok("撤回后不被召回", all(f["id"] != new_id for f in after["facts"]))
        md_files = list((temp / "http" / "memory").rglob("*.md"))
        ok("撤回墓碑落盘且不改写事实正文", any("tombstone: [RETRACTED:" in p.read_text(encoding="utf-8")
                                          and "NEW-VALUE" in p.read_text(encoding="utf-8") for p in md_files))

        before = request(url + "/v1/status")[1]["status"]["events"]
        shadow_event = event("shadow-1", "仅捕获，不形成记忆", host="dsh")
        status, shadow = request(url + "/v1/shadow", "POST", {
            "event": shadow_event, "query": "K003", "baselineFactIds": ["memorix-base-1"]})
        ok("影子捕获不注入、不写事实", status == 200 and not shadow["result"]["injected"]
           and not shadow["result"]["factsWritten"])
        _, shadow_dup = request(url + "/v1/shadow", "POST", {"event": shadow_event})
        after_shadow = request(url + "/v1/status")[1]["status"]["events"]
        journal = (temp / "http" / "shadow.jsonl").read_text(encoding="utf-8").splitlines()
        ok("影子事件幂等且事实数不变", shadow_dup["result"]["duplicate"] and before == after_shadow
           and len(journal) == 1, len(journal))
        logged = json.loads(journal[0])
        ok("影子日志含双侧 ID 与延迟", logged["baselineFactIds"] == ["memorix-base-1"]
           and "recallFactIds" in logged and "latencyMs" in logged)

        status, bad = request(url + "/v1/events", "POST", {
            "event_id": "bad", "project": "I:\\example-project", "subject": "alice", "text": "越界",
            "trust": "direct", "confirmed": True})
        ok("绝对路径项目键被拒绝", status == 400 and not bad["ok"])
        # The direct-write trust boundary: reachability must not imply approval.
        status, untrusted = request(url + "/v1/events", "POST",
                                    {"event_id": "no-trust", "project": "p1", "subject": "alice", "text": "未经声明的直写"})
        ok("未声明 trust 的直写被拒绝", status == 400 and not untrusted["ok"], untrusted)
        status, no_confirm = request(url + "/v1/events", "POST",
                                     {"event_id": "no-confirm", "project": "p1", "subject": "alice",
                                      "text": "未声明 confirmed 的直写", "trust": "direct"})
        ok("未显式声明 confirmed 的直写被拒绝", status == 400 and not no_confirm["ok"], no_confirm)
        ok("没有路径越界写入", [p.name for p in (temp / "http" / "memory").iterdir()] == ["p1"])
    finally:
        service.close()

    restarted = BridgeService(temp / "http", port=0).start()
    try:
        _, state = request(restarted.url + "/v1/status")
        ok("HTTP 服务重启后持久", state["status"]["events"] == 25 and state["status"]["facts_total"] == 25)
    finally:
        restarted.close()

    # Crash before Markdown: queue has the full payload, but no fact file/index/event row.
    crash = temp / "crash-before-md"
    store = Store(crash)
    store.close()
    db = sqlite3.connect(crash / "index.db")
    payload = event("crash-1", "崩溃前只有 pending payload")
    fact_id = "f_" + uuid.uuid5(uuid.NAMESPACE_URL, "crash-1").hex[:16]
    db.execute("INSERT INTO queue(event_id,fact_id,status,updated_at,payload) VALUES (?,?,?,?,?)",
               ("crash-1", fact_id, "pending", now_iso(), json.dumps(payload, ensure_ascii=False)))
    db.commit()
    db.close()
    recovered = Store(crash)
    ok("崩溃在 Markdown 之前也能恢复", recovered.recovery["recovered"] == 1
       and recovered.status()["facts_total"] == 1 and recovered.status()["queue_pending"] == 0)
    ok("恢复后重复事件仍幂等", recovered.append(payload)["duplicate"])
    recovered.close()

    # Crash after Markdown but before index/event commit, with a supersession to replay.
    crash2 = temp / "crash-after-md"
    store = Store(crash2)
    old = store.append(event("base", "旧偏好"))["fact_id"]
    rev = event("crash-2", "新偏好", supersedes=old)
    new_fact = store._fact_from_event(rev)
    raw = json.dumps(rev, ensure_ascii=False)
    store.db.execute("INSERT INTO queue(event_id,fact_id,status,updated_at,payload) VALUES (?,?,?,?,?)",
                     ("crash-2", new_fact["id"], "pending", now_iso(), raw))
    store.db.commit()
    store._write_fact(new_fact)
    store.close()  # simulates process death here
    recovered = Store(crash2)
    current = recovered.query("p1", "alice")
    ok("崩溃在索引提交前可恢复并回放偏好变更", recovered.recovery["recovered"] == 1
       and [f["text"] for f in current] == ["新偏好"], [f["text"] for f in current])
    ok("恢复后无临时文件残留", not list((crash2 / "memory").rglob("*.tmp-*")))
    recovered.close()

    print(f"\n结果: {sum(checks)}/{len(checks)} 通过")
    if len(checks) != sum(checks):
        raise SystemExit(1)
    shutil.rmtree(temp)


if __name__ == "__main__":
    main()
