"""Candidate -> human review -> current fact workflow tests."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import BridgeService  # noqa: E402
from memlayer import Store  # noqa: E402

checks = []


def ok(name, condition, detail=""):
    checks.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))


def request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(quote(url, safe=":/?&=%"), data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main():
    temp = Path(tempfile.mkdtemp(prefix="candidate-test-"))
    service = BridgeService(temp / "http", port=0).start()
    url = service.url
    try:
        shadow = {"event_id": "shadow-review-1", "project": "p1", "subject": "user",
                  "text": "原始对话内容", "perspective": "self", "confirmed": False, "host": "dsh"}
        request(url + "/v1/shadow", "POST", {"event": shadow})
        status, created = request(url + "/v1/candidates/from-shadow", "POST", {
            "event_id": "shadow-review-1", "text": "回答优先使用中文"})
        candidate = created["result"]["candidate"]
        ok("影子事件可生成待审核候选", status == 200 and candidate["status"] == "pending")
        ok("候选生成时不形成事实", candidate["fact_id"] is None)
        _, pending = request(url + "/v1/candidates?status=pending&project=p1")
        ok("待审核列表包含候选", len(pending["candidates"]) == 1)
        _, before = request(url + "/v1/facts?project=p1&q=中文")
        ok("待审核候选不参与召回", before["facts"] == [])

        status, conflict = request(url + "/v1/candidates/approve", "POST", {
            "candidate_id": candidate["id"], "expected_hash": "stale"})
        ok("过期内容哈希审批返回 409", status == 409 and conflict["error"]["code"] == "review_conflict")
        status, approved = request(url + "/v1/candidates/approve", "POST", {
            "candidate_id": candidate["id"], "expected_hash": candidate["content_hash"],
            "note": "人工确认：可作为长期偏好"})
        ok("人工批准后生成当前事实", status == 200 and approved["result"]["fact_id"])
        fact_id = approved["result"]["fact_id"]
        _, recalled = request(url + "/v1/facts?project=p1&q=中文")
        ok("批准后可召回且带来源", len(recalled["facts"]) == 1
           and recalled["facts"][0]["confirmed"] is True
           and recalled["facts"][0]["source_ids"] == ["shadow-review-1"])
        status, duplicate = request(url + "/v1/candidates/approve", "POST", {
            "candidate_id": candidate["id"], "expected_hash": candidate["content_hash"]})
        _, state = request(url + "/v1/status")
        ok("重复审批不重复创建事实", duplicate["result"]["duplicate"] is True
           and state["status"]["facts_total"] == 1)

        direct = {"project": "p1", "subject": "user", "text": "不应生效的候选",
                  "source_event_id": "manual-2", "perspective": "self"}
        _, second = request(url + "/v1/candidates", "POST", direct)
        second_candidate = second["result"]["candidate"]
        _, rejected = request(url + "/v1/candidates/reject", "POST", {
            "candidate_id": second_candidate["id"],
            "expected_hash": second_candidate["content_hash"], "note": "不采用"})
        ok("人工拒绝后候选为 rejected", rejected["result"]["candidate"]["status"] == "rejected")
        _, facts = request(url + "/v1/facts?project=p1&q=不应生效")
        ok("拒绝候选永不参与召回", facts["facts"] == [])
        status, invalid = request(url + "/v1/candidates/reject", "POST", {
            "candidate_id": candidate["id"], "expected_hash": candidate["content_hash"]})
        ok("已批准候选不能再拒绝", status == 409 and invalid["ok"] is False)
        _, dup_candidate = request(url + "/v1/candidates", "POST", direct)
        ok("重复候选创建幂等", dup_candidate["result"]["duplicate"] is True)

        md = (temp / "http" / "candidates" / "p1" / (candidate["id"] + ".md")).read_text(encoding="utf-8")
        ok("候选 Markdown 保存审核轨迹", "status: approved" in md and "人工确认" in md
           and "回答优先使用中文" in md)
    finally:
        service.close()

    restarted = BridgeService(temp / "http", port=0).start()
    try:
        _, state = request(restarted.url + "/v1/status")
        ok("重启后审批状态与事实均保留", state["status"]["facts_total"] == 1
           and state["status"]["candidates_approved"] == 1
           and state["status"]["candidates_rejected"] == 1)
        _, rebuilt = request(restarted.url + "/v1/rebuild", "POST", {})
        _, state2 = request(restarted.url + "/v1/status")
        ok("从 Markdown 重建后候选不丢", state2["status"]["candidates_approved"] == 1
           and state2["status"]["candidates_rejected"] == 1)
    finally:
        restarted.close()

    # Staging semantics: the candidate path must never write memory/ before approval,
    # and a direct caller-authoritative append stays independent of review state.
    crash = temp / "staging-semantics"
    store = Store(crash)
    created = store.create_candidate({"project": "p1", "subject": "user", "text": "暂存语义候选",
                                      "source_event_id": "staging-candidate"})["candidate"]
    ok("候选在批准前不写 memory 目录",
       not list((crash / "memory").rglob("*.md")) and store.status()["staging"] == 0)
    direct = store.append({"event_id": "direct-1", "project": "p1", "subject": "user",
                           "text": "调用方权威写入", "confirmed": True})
    approved = store.approve_candidate(created["id"], created["content_hash"])
    ok("直接写入与审批事实互不干扰", store.status()["facts_total"] == 2
       and approved["fact_id"] != direct["fact_id"] and store.status()["staging"] == 0)
    store.close()

    print(f"\n结果: {sum(checks)}/{len(checks)} 通过")
    if len(checks) != sum(checks):
        raise SystemExit(1)
    shutil.rmtree(temp)


if __name__ == "__main__":
    main()
