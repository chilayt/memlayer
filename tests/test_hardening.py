"""Regression tests for Codex-reviewed correctness blockers."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import MutationWriter  # noqa: E402
from memlayer import ReviewConflict, Store  # noqa: E402

checks = []


def ok(name, condition, detail=""):
    checks.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))


def candidate_payload(project="p1", text="候选", source="source-1"):
    return {"project": project, "subject": "user", "text": text,
            "source_event_id": source, "perspective": "self"}


def main():
    temp = Path(tempfile.mkdtemp(prefix="hardening-test-"))
    try:
        # Approval failure after fact creation: rejection must tombstone the orphan.
        store = Store(temp / "approval")
        created = store.create_candidate(candidate_payload())["candidate"]
        original_write = store._write_candidate
        def fail_write(_candidate):
            raise OSError("injected candidate write failure")
        store._write_candidate = fail_write
        try:
            store.approve_candidate(created["id"], created["content_hash"], "approved")
        except OSError:
            pass
        ok("审批故障时未批准事实完全不可见（staging）",
           store.get_candidate(created["id"])["status"] == "pending" and store.query("p1") == []
           and store.query("p1", mode="all") == [] and store.status()["staging"] == 1)
        store._write_candidate = original_write
        store.reject_candidate(created["id"], created["content_hash"], "reject staged")
        ok("拒绝候选会销毁 staging 副本", store.get_candidate(created["id"])["status"] == "rejected"
           and store.query("p1", mode="all") == [] and store.status()["staging"] == 0)

        # Approving the same interrupted operation must recover without a duplicate.
        created2 = store.create_candidate(candidate_payload(text="审批恢复", source="source-2"))["candidate"]
        store._write_candidate = fail_write
        try:
            store.approve_candidate(created2["id"], created2["content_hash"])
        except OSError:
            pass
        store._write_candidate = original_write
        recovered = store.approve_candidate(created2["id"], created2["content_hash"])
        ok("中断审批重试幂等恢复", recovered["candidate"]["status"] == "approved"
           and store.status()["facts_total"] == 1 and store.status()["staging"] == 0
           and len(store.query("p1")) == 1)

        # None note is normalized, never an AttributeError.
        created3 = store.create_candidate(candidate_payload(text="空备注", source="source-3"))["candidate"]
        approved3 = store.approve_candidate(created3["id"], created3["content_hash"], None)
        ok("空审批备注不会留下异常孤儿", approved3["candidate"]["status"] == "approved"
           and approved3["candidate"]["review_note"] == "")
        store.close()

        # Editing Markdown invalidates the old review hash and identity.
        store = Store(temp / "approval")
        created4 = store.create_candidate(candidate_payload(text="原文", source="source-4"))["candidate"]
        path = store._candidate_path(created4)
        raw = path.read_text(encoding="utf-8").replace("原文", "被外部改写的正文")
        path.write_text(raw, encoding="utf-8")
        before_stale = store.status()["facts_total"]
        try:
            store.approve_candidate(created4["id"], created4["content_hash"])
            ok("外部修改正文后旧哈希被拒绝", False)
        except ReviewConflict:
            ok("外部修改正文后旧哈希被拒绝", store.status()["facts_total"] == before_stale
               and all(f["text"] != "被外部改写的正文" for f in store.query("p1", mode="all")))
        store.close()

        # Retraction and event ledger both survive a complete index loss.
        store = Store(temp / "retract")
        event = {"event_id": "old-event", "project": "p1", "subject": "user", "text": "不要复活"}
        fact_id = store.append(event)["fact_id"]
        store.retract(fact_id, "forget")
        store.close()
        for path in (temp / "retract").glob("index.db*"):
            path.unlink()
        store = Store(temp / "retract")
        replay = store.append(event)
        ok("丢整个 SQLite 后旧事件仍幂等", replay["duplicate"] is True)
        ok("丢整个 SQLite 后撤回不复活", store.query("p1") == []
           and store.query("p1", mode="all") == []
           and store.get_fact(replay["fact_id"])["status"] == "retracted"
           and store.status()["events"] == 1)
        store.close()

        # Project-scoped ids permit identical event ids in separate projects,
        # but forbid cross-project supersession.
        store = Store(temp / "namespace")
        one = store.append({"event_id": "same", "project": "p1", "subject": "user", "text": "A"})
        two = store.append({"event_id": "same", "project": "p2", "subject": "user", "text": "B"})
        ok("相同事件 ID 可安全存在于不同项目", one["fact_id"] != two["fact_id"]
           and len(store.query("p1")) == 1 and len(store.query("p2")) == 1)
        ca = store.create_candidate(candidate_payload("p1", "same-text", "same-source"))["candidate"]
        cb = store.create_candidate(candidate_payload("p2", "same-text", "same-source"))["candidate"]
        ok("候选 ID 含项目作用域", ca["id"] != cb["id"] and cb["project"] == "p2")
        try:
            store.supersede(one["fact_id"], {"event_id": "cross", "project": "p2",
                                             "subject": "user", "text": "cross"})
            ok("跨项目偏好变更被拒绝", False)
        except ValueError:
            ok("跨项目偏好变更被拒绝", len(store.query("p1")) == 1)
        store.close()

        # A slow writer must never cause close() to close SQLite underneath it.
        store = Store(temp / "shutdown")
        lock = threading.RLock()
        writer = MutationWriter(store, lock, capacity=2)
        original_append = store.append
        def slow_append(event):
            time.sleep(0.4)
            return original_append(event)
        store.append = slow_append
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer.submit, "append",
                                   {"event_id": f"slow-{i}", "project": "p1", "subject": "user", "text": str(i)})
                       for i in range(2)]
            started = time.monotonic()
            try:
                writer.close(timeout=0.05)
                ok("慢写时关闭有界且不关库", False)
            except TimeoutError:
                ok("慢写时关闭有界且不关库", time.monotonic() - started < 0.3
                   and writer.thread.is_alive() and store.status()["facts_total"] >= 0)
            store.append = original_append
            writer.close(timeout=2.0)
            for future in futures:
                future.result(timeout=1)
        ok("慢写结束后可重试安全关闭", not writer.thread.is_alive())
        store.close()

        print(f"\n结果: {sum(checks)}/{len(checks)} 通过")
        if len(checks) != sum(checks):
            raise SystemExit(1)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
