"""Staged-commit protocol tests: unapproved facts must never be visible."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import BridgeService  # noqa: E402
from memlayer import ReviewConflict, Store  # noqa: E402

checks = []


def ok(name, condition, detail=""):
    checks.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))


def request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(urllib.parse.quote(url, safe=":/?&=%"), data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


import urllib.parse  # noqa: E402  (used by request)


def payload_for(candidate):
    return {"project": candidate["project"], "subject": candidate["subject"],
            "text": candidate["text"], "source_event_id": candidate["source_event_id"]}


def main():
    temp = Path(tempfile.mkdtemp(prefix="staging-test-"))
    try:
        # 1) Crash between staging write and candidate update: nothing visible.
        store = Store(temp / "c1")
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "阶段事实",
                                          "source_event_id": "s1"})["candidate"]
        original = store._write_candidate
        store._write_candidate = lambda c: (_ for _ in ()).throw(OSError("injected"))
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._write_candidate = original
        ok("审批中断：候选 pending 且事实完全不可见",
           store.get_candidate(created["id"])["status"] == "pending"
           and store.query("p1") == [] and store.query("p1", mode="all") == []
           and store.status()["staging"] == 1)
        store.reject_candidate(created["id"], created["content_hash"], "no")
        ok("拒绝销毁 staging 副本", store.status()["staging"] == 0
           and store.query("p1", mode="all") == []
           and store.get_candidate(created["id"])["status"] == "rejected")
        store.close()

        # 2) Crash between candidate approval and commit: invisible until recovery completes it.
        store = Store(temp / "c2")
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "恢复事实",
                                          "source_event_id": "s2"})["candidate"]
        original_commit = store._commit_staged_fact
        store._commit_staged_fact = lambda f: (_ for _ in ()).throw(OSError("injected"))
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._commit_staged_fact = original_commit
        ok("已批准但未提交：仍不可见", store.query("p1") == [] and store.status()["staging"] == 1
           and store.get_candidate(created["id"])["status"] == "approved")
        store.close()
        reopened = Store(temp / "c2")
        ok("重启后恢复提交并可见", reopened.query("p1") and reopened.status()["staging"] == 0
           and reopened.status()["events"] == 1)
        reopened.close()

        # 3) Crash between candidate Markdown and candidate index: recovery still commits.
        store = Store(temp / "c3")
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "索引崩溃",
                                          "source_event_id": "s3"})["candidate"]
        original_index = store._index_candidate
        def boom(candidate):
            if candidate.get("status") == "approved":
                raise OSError("injected")
            return original_index(candidate)
        store._index_candidate = boom
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._index_candidate = original_index
        ok("索引崩溃后事实仍不可见", store.query("p1") == [])
        store.close()
        reopened = Store(temp / "c3")
        ok("Markdown 为准：重启后批准态与事实均恢复",
           reopened.get_candidate(created["id"])["status"] == "approved"
           and len(reopened.query("p1")) == 1 and reopened.status()["staging"] == 0)
        reopened.close()

        # 4) rebuild() completes an approved-but-uncommitted staged fact.
        store = Store(temp / "c4")
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "重建事实",
                                          "source_event_id": "s4"})["candidate"]
        original_commit = store._commit_staged_fact
        store._commit_staged_fact = lambda f: (_ for _ in ()).throw(OSError("injected"))
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._commit_staged_fact = original_commit
        result = store.rebuild()
        ok("rebuild 完成已批准的 staging 提交", result["staging"]["moved"] == 1
           and len(store.query("p1")) == 1 and store.status()["staging"] == 0)
        store.close()

        # 5) Concurrent approve/reject through the bridge single writer: one legal terminal state.
        service = BridgeService(temp / "c5", port=0).start()
        url = service.url
        _, created = request(url + "/v1/candidates", "POST", {
            "project": "p1", "subject": "user", "text": "并发审批", "source_event_id": "s5"})
        candidate = created["result"]["candidate"]
        results = []
        def approve():
            results.append(request(url + "/v1/candidates/approve", "POST",
                                   {"candidate_id": candidate["id"], "expected_hash": candidate["content_hash"]}))
        def reject():
            results.append(request(url + "/v1/candidates/reject", "POST",
                                   {"candidate_id": candidate["id"], "expected_hash": candidate["content_hash"]}))
        threads = [threading.Thread(target=approve), threading.Thread(target=reject)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _, state = request(url + "/v1/status")
        _, listed = request(url + "/v1/candidates?project=p1")
        status = listed["candidates"][0]["status"]
        visible = len((request(url + "/v1/facts?project=p1&mode=all")[1])["facts"])
        ok("并发批准/拒绝只有一个合法终态且可见性一致",
           status in ("approved", "rejected")
           and ((status == "approved" and visible == 1) or (status == "rejected" and visible == 0))
           and state["status"]["staging"] == 0, f"status={status} visible={visible}")
        service.close()

        # 5) Crash between staging move and fact indexing: reconcile repairs.
        store = Store(temp / "c6")
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "移动后索引前崩溃",
                                          "source_event_id": "s6"})["candidate"]
        original_index = store._index
        store._index = lambda f: (_ for _ in ()).throw(OSError("injected"))
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._index = original_index
        store.close()
        reopened = Store(temp / "c6")
        ok("move 后索引前崩溃：重启补齐索引与事件", len(reopened.query("p1")) == 1
           and reopened.status()["events"] == 1 and reopened.status()["staging"] == 0)
        reopened.close()

        # 6) Crash between fact indexing and event recording: reconcile repairs ledger.
        store = Store(temp / "c7")
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "索引后事件前崩溃",
                                          "source_event_id": "s7"})["candidate"]
        original_record = store._record_event
        store._record_event = lambda f, p=None: (_ for _ in ()).throw(OSError("injected"))
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._record_event = original_record
        store.close()
        reopened = Store(temp / "c7")
        ok("索引后事件前崩溃：重启补齐事件账", reopened.status()["events"] == 1
           and len(reopened.query("p1")) == 1)
        reopened.close()

        # 7) Approved staging whose source is retracted before commit stays blocked.
        store = Store(temp / "c8")
        src_fact = store.append({"event_id": "e8", "project": "p1", "subject": "user",
                                 "text": "来源事实", "source_msg": "src-8"})["fact_id"]
        created = store.create_candidate({"project": "p1", "subject": "user", "text": "批准后被来源撤回",
                                          "source_event_id": "src-8"})["candidate"]
        original_commit = store._commit_staged_fact
        store._commit_staged_fact = lambda f: (_ for _ in ()).throw(OSError("injected"))
        try:
            store.approve_candidate(created["id"], created["content_hash"])
        except OSError:
            pass
        store._commit_staged_fact = original_commit
        store.retract(src_fact, "forget source before commit")
        store.close()
        reopened = Store(temp / "c8")
        ok("来源撤回后已批准 staging 不提交", reopened.query("p1") == []
           and reopened.status()["staging"] == 1
           and reopened.staging_recovery.get("blocked") == 1)
        reopened.close()

        # 8) Case 1 restart: rejected candidate leaves no staging and no fact.
        store = Store(temp / "c1")
        ok("拒绝后重启仍无 staging 与事实", store.status()["staging"] == 0
           and store.query("p1", mode="all") == [])
        store.close()

        service = BridgeService(temp / "replace", port=0).start()
        url = service.url
        def author_candidate(text, source, **extra):
            return request(url + "/v1/candidates", "POST", {
                "project": "p1", "subject": "author-note", "text": text,
                "source_event_id": source, **extra,
            })[1]["result"]["candidate"]

        old_candidate = author_candidate("交接标题以旧词开头", "old")
        old_id = request(url + "/v1/candidates/approve", "POST", {
            "candidate_id": old_candidate["id"], "expected_hash": old_candidate["content_hash"],
        })[1]["result"]["fact_id"]
        new_candidate = author_candidate("交接标题以新词开头", "new", suggested_fact_id=old_id)
        visible = request(url + "/v1/facts?project=p1&offset=0&filter=" + old_id)[1]["facts"]
        ok("待审更正不影响旧条目，核对页可按编号查找旧条目",
           new_candidate["suggested_fact_id"] == old_id and [item["id"] for item in visible] == [old_id])
        payload = {"candidate_id": new_candidate["id"], "expected_hash": new_candidate["content_hash"],
                   "supersedes_fact_id": old_id}
        _, first = request(url + "/v1/candidates/approve-replacement", "POST", payload)
        _, repeated = request(url + "/v1/candidates/approve-replacement", "POST", payload)
        http, _ = request(url + "/v1/candidates/approve", "POST", payload)
        current = request(url + "/v1/facts?project=p1")[1]["facts"]
        history = request(url + "/v1/facts?project=p1&mode=all")[1]["facts"]
        new_id = first["result"]["fact_id"]
        ok("核对替换后，旧条目留档，重复点击不产生条目或改成新增",
           [item["id"] for item in current] == [new_id]
           and {item["id"]: item["status"] for item in history} == {old_id: "superseded", new_id: "current"}
           and repeated["result"]["fact_id"] == new_id and repeated["result"]["duplicate"] and http == 409)
        service.close()

        store = Store(temp / "replacement-recovery")
        old_id = store.append({"event_id": "base", "project": "p1", "subject": "author-note",
                               "text": "旧习惯", "confirmed": True})["fact_id"]
        created = store.create_candidate({"project": "p1", "subject": "author-note", "text": "新习惯",
                                          "source_event_id": "new", "suggested_fact_id": old_id})["candidate"]
        invalid = store.append({"event_id": "other", "project": "p2", "subject": "author-note",
                                "text": "别的作品的习惯", "confirmed": True})["fact_id"]
        try:
            store.approve_candidate(created["id"], created["content_hash"], supersedes_fact_id=invalid)
        except ReviewConflict:
            ok("不能替换另一作品的条目", store.get_candidate(created["id"])["status"] == "pending")
        else:
            ok("不能替换另一作品的条目", False)
        close_fact = store._close_fact
        def crash_after_close(*args, **kwargs):
            close_fact(*args, **kwargs)
            raise OSError("injected after closing old fact")
        store._close_fact = crash_after_close
        try:
            store.approve_candidate(created["id"], created["content_hash"], supersedes_fact_id=old_id)
        except OSError:
            pass
        store.close()
        reopened = Store(temp / "replacement-recovery")
        record = reopened.get_candidate(created["id"])
        ok("替换在关闭旧条目后中断，重开只发布已确认的新条目",
           record["status"] == "approved" and reopened.status()["staging"] == 0
           and [item["id"] for item in reopened.query("p1")] == [record["fact_id"]]
           and reopened.get_fact(old_id)["status"] == "superseded")
        reopened.close()

        service = BridgeService(temp / "exception", port=0).start()
        url = service.url
        base = author_candidate("普通巡夜交接以夜巡开头", "original")
        base_id = request(url + "/v1/candidates/approve", "POST", {
            "candidate_id": base["id"], "expected_hash": base["content_hash"],
        })[1]["result"]["fact_id"]
        exception = author_candidate("仅在卷末，巡夜交接以卷终开头", "exception",
                                     suggested_fact_id=base_id, suggested_action="exception")
        payload = {"candidate_id": exception["id"], "expected_hash": exception["content_hash"],
                   "supersedes_fact_id": base_id}
        before = request(url + "/v1/facts?project=p1&offset=0")[1]["facts"]
        ok("待审例外保留现行默认规则", exception["suggested_action"] == "exception"
           and [item["text"] for item in before] == [base["text"]])
        _, first = request(url + "/v1/candidates/approve-exception", "POST", payload)
        _, rep = request(url + "/v1/candidates/approve-exception", "POST", payload)
        conflict, _ = request(url + "/v1/candidates/approve-replacement", "POST", payload)
        rows = request(url + "/v1/facts?project=p1&offset=0")[1]["facts"]
        ok("批准例外合并原规则、限制条件和两处来源，重试幂等",
           len(rows) == 1 and rows[0]["id"] == first["result"]["fact_id"]
           and all(text in rows[0]["text"] for text in (base["text"], exception["text"]))
           and set(rows[0]["source_ids"]) == {"original", "exception"}
           and rep["result"]["duplicate"] and conflict == 409)
        service.close()
        reopened = Store(temp / "exception")
        original_candidate = reopened.get_candidate(base["id"])
        ok("旧候选的空核对提示重开后仍为空",
           original_candidate["suggested_fact_id"] is None and original_candidate["suggested_action"] is None)
        ok("重开后仅组合规则生效，撤回原出处时组合规则一并撤回",
           [item["id"] for item in reopened.query("p1")] == [first["result"]["fact_id"]]
           and reopened.retract(base_id, "source withdrawn") and reopened.query("p1") == [])
        reopened.close()

        print(f"\n结果: {sum(checks)}/{len(checks)} 通过")
        if len(checks) != sum(checks):
            raise SystemExit(1)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
