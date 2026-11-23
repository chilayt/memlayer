"""Retraction ledger, cascade, backup-restore replay, and crash-order tests."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from bridge import BridgeService  # noqa: E402
from memlayer import ReviewConflict, Store  # noqa: E402

checks = []


def ok(name, condition, detail=""):
    checks.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))


def main():
    temp = Path(tempfile.mkdtemp(prefix="retraction-test-"))
    try:
        store = Store(temp / "r1")
        fact = store.append({"event_id": "e1", "project": "p1", "subject": "user",
                             "text": "将被撤回的事实", "source_msg": "src-1"})["fact_id"]
        other = store.append({"event_id": "e2", "project": "p1", "subject": "user",
                              "text": "无关事实", "source_msg": "src-2"})["fact_id"]
        blocked = store.create_candidate({"project": "p1", "subject": "user", "text": "来自被撤回来源",
                                          "source_event_id": "src-1"})["candidate"]
        safe = store.create_candidate({"project": "p1", "subject": "user", "text": "来自正常来源",
                                       "source_event_id": "src-2"})["candidate"]
        ok("撤回前候选可批准", store.approve_candidate(safe["id"], safe["content_hash"])["candidate"]["status"] == "approved")
        ok("撤回事实", store.retract(fact, "用户要求忘记"))
        ok("重复撤回只产生一条账", len(list((temp / "r1" / "retractions").rglob("*.md"))) == 1)
        try:
            store.approve_candidate(blocked["id"], blocked["content_hash"])
            ok("被撤回来源的候选禁止批准", False)
        except ReviewConflict:
            ok("被撤回来源的候选禁止批准", True)
        ok("无关来源候选不受影响", store.get_candidate(safe["id"])["status"] == "approved")
        ok("拒绝被阻断候选仍可行", store.reject_candidate(blocked["id"], blocked["content_hash"])["candidate"]["status"] == "rejected")
        safe_fact = store.get_candidate(safe["id"])["fact_id"]
        ok("撤回后 current 与召回不含该事实",
           {f["id"] for f in store.query("p1")} == {other, safe_fact})
        allmode = {f["id"] for f in store.query("p1", mode="all")}
        md = (temp / "r1" / "memory" / "p1" / f"{fact}.md").read_text(encoding="utf-8")
        ok("已撤回事实不从 all 模式回传（审计读也不泄漏原文）", fact not in allmode, sorted(allmode))
        ok("墓碑落盘供审计且带撤回标记",
           store.get_fact(fact)["status"] == "retracted" and "tombstone: [RETRACTED:" in md)

        # Same-source cascade must reach superseded versions too: an old version
        # that shares the source may not keep serving via mode=all after retraction.
        cascade_store = Store(temp / "r-cascade")
        old_v = cascade_store.append({"event_id": "e90", "project": "p1", "subject": "user",
                                      "text": "旧版偏好 SOURCE-SHARED-OLD", "source_msg": "src-90"})["fact_id"]
        new_v = cascade_store.append({"event_id": "e91", "project": "p1", "subject": "user",
                                      "text": "新版偏好 SOURCE-SHARED-NEW", "source_msg": "src-90",
                                      "supersedes": old_v})["fact_id"]
        keep_v = cascade_store.append({"event_id": "e92", "project": "p1", "subject": "user",
                                       "text": "其他来源事实", "source_msg": "src-91"})["fact_id"]
        ok("替换后旧版为 superseded", cascade_store.get_fact(old_v)["status"] == "superseded")
        cascade_store.retract(new_v, "撤回同源来源")
        ok("级联撤回覆盖 superseded 旧版", cascade_store.get_fact(old_v)["status"] == "retracted",
           cascade_store.get_fact(old_v)["status"])
        all_after = {f["id"] for f in cascade_store.query("p1", mode="all")}
        ok("mode=all 不再返回同源旧版原文", old_v not in all_after)
        ok("同源旧版保留审计墓碑",
           "tombstone: [RETRACTED:" in (temp / "r-cascade" / "memory" / "p1" / f"{old_v}.md").read_text(encoding="utf-8"))
        ok("无关来源事实未受级联影响", cascade_store.get_fact(keep_v)["status"] == "current")
        cascade_store.close()

        # Crash between ledger write and tombstone: replay must repair.
        store2 = Store(temp / "r2")
        f2 = store2.append({"event_id": "e10", "project": "p1", "subject": "user",
                            "text": "账先墓碑后崩溃", "source_msg": "src-10"})["fact_id"]
        original_close = store2._close_fact
        store2._close_fact = lambda *a, **k: (_ for _ in ()).throw(OSError("injected"))
        try:
            store2.retract(f2, "forget")
        except (OSError, RuntimeError):
            pass  # close failed and in-call replay could not repair: ledger stays durable
        store2._close_fact = original_close
        ok("账已写、墓碑未写时事实仍暂时可见但账存在",
           store2.get_fact(f2)["status"] == "current" and store2._ledger_for(f2) is not None)
        store2.close()
        reopened2 = Store(temp / "r2")
        ok("重启重放补齐墓碑", reopened2.get_fact(f2)["status"] == "retracted"
           and reopened2.query("p1") == [])
        reopened2.close()

        # Backup-restore resurrection with a backup that CONTAINS the live fact.
        store3 = Store(temp / "r3")
        live = store3.append({"event_id": "e20", "project": "p1", "subject": "user",
                              "text": "备份内含该事实", "source_msg": "src-20"})["fact_id"]
        backup = temp / "memory-backup-r3"
        shutil.copytree(store3.memory, backup)
        store3.retract(live, "forget after backup")
        store3.close()
        shutil.rmtree(temp / "r3" / "memory")
        shutil.copytree(backup, temp / "r3" / "memory")
        restored = Store(temp / "r3")
        ok("旧备份（含该事实）恢复后撤回账重放生效",
           restored.get_fact(live)["status"] == "retracted" and restored.query("p1") == [])
        restored.close()

        # Cascade: approved derived fact sharing the source is retracted too.
        store4 = Store(temp / "r4")
        src_fact = store4.append({"event_id": "e30", "project": "p1", "subject": "user",
                                  "text": "原始捕获", "source_msg": "src-30"})["fact_id"]
        derived = store4.create_candidate({"project": "p1", "subject": "user",
                                           "text": "派生批准事实", "source_event_id": "src-30"})["candidate"]
        store4.approve_candidate(derived["id"], derived["content_hash"])
        ok("级联前派生事实可见", len(store4.query("p1")) == 2)
        store4.retract(src_fact, "forget source")
        ok("撤回来源级联撤回同源派生事实", store4.query("p1") == []
           and store4.get_fact(src_fact)["status"] == "retracted"
           and store4.get_fact(derived["fact_id"] or store4.get_candidate(derived["id"])["fact_id"])["status"] == "retracted"
           and store4.query("p1", mode="all") == [])
        store4.close()

        # Same source id in another project must not be blocked or cascaded.
        store5 = Store(temp / "r5")
        pa = store5.append({"event_id": "dup", "project": "p1", "subject": "user",
                            "text": "项目一事实", "source_msg": "dup-src"})["fact_id"]
        pb = store5.append({"event_id": "dup", "project": "p2", "subject": "user",
                            "text": "项目二事实", "source_msg": "dup-src"})["fact_id"]
        cand2 = store5.create_candidate({"project": "p2", "subject": "user",
                                         "text": "项目二候选", "source_event_id": "dup-src"})["candidate"]
        store5.retract(pa, "forget p1 only")
        ok("跨项目相同来源互不误伤", store5.get_fact(pb)["status"] == "current"
           and store5.approve_candidate(cand2["id"], cand2["content_hash"])["candidate"]["status"] == "approved")
        store5.close()

        # Index loss keeps retraction through Markdown truth plus ledger.
        store.close()
        for p in (temp / "r1").glob("index.db*"):
            p.unlink()
        store6 = Store(temp / "r1")
        ok("丢索引后撤回状态保持", all(f["id"] != fact for f in store6.query("p1")))
        store6.close()

        # Supersede chain: retracting either end never revives the other.
        store7 = Store(temp / "r7")
        old = store7.append({"event_id": "s-old", "project": "p2", "subject": "user", "text": "旧偏好"})["fact_id"]
        new = store7.supersede(old, {"event_id": "s-new", "project": "p2", "subject": "user", "text": "新偏好"})["fact_id"]
        store7.retract(old, "retract superseded old")
        ok("撤回被替代旧事实不影响新事实", [f["id"] for f in store7.query("p2")] == [new])
        store7.retract(new, "retract current new")
        statuses = {f["status"] for f in store7.query("p2", mode="all")}
        ok("撤回新事实不复活旧事实", store7.query("p2") == [] and "current" not in statuses, statuses)
        store7.close()

        # Self-heal: a close failure after the ledger is written must be repaired in-call.
        store8 = Store(temp / "r8")
        f8 = store8.append({"event_id": "e40", "project": "p1", "subject": "user",
                            "text": "自愈事实", "source_msg": "src-40"})["fact_id"]
        calls = {"n": 0}
        original8 = store8._close_fact
        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("injected")
            return original8(*args, **kwargs)
        store8._close_fact = flaky
        ok("写账后关闭失败：同调用内自愈", store8.retract(f8, "forget") is True
           and store8.get_fact(f8)["status"] == "retracted")
        store8._close_fact = original8
        store8.close()

        # Cascade crash replay: ledger durable, tombstones applied on next startup.
        store9 = Store(temp / "r9")
        src9 = store9.append({"event_id": "e41", "project": "p1", "subject": "user",
                              "text": "级联来源", "source_msg": "src-41"})["fact_id"]
        derived9 = store9.create_candidate({"project": "p1", "subject": "user",
                                            "text": "级联派生", "source_event_id": "src-41"})["candidate"]
        approved9 = store9.approve_candidate(derived9["id"], derived9["content_hash"])
        derived9_fact = approved9["fact_id"]
        store9._close_fact = lambda *a, **k: (_ for _ in ()).throw(OSError("injected"))
        raised = False
        try:
            store9.retract(src9, "forget all")
        except RuntimeError:
            raised = True
        ok("级联关闭全失败时显式报错而非静默", raised)
        store9.close()
        reopened9 = Store(temp / "r9")
        ok("重启重放同时关闭主事实与级联事实", reopened9.query("p1") == []
           and reopened9.get_fact(src9)["status"] == "retracted"
           and reopened9.get_fact(derived9_fact)["status"] == "retracted"
           and reopened9.query("p1", mode="all") == [])
        reopened9.close()

        # Semantic ledger corruption must fail startup, not silently no-op.
        store10 = Store(temp / "r10")
        bad = temp / "r10" / "retractions" / "p1" / "r_bad.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("---\nid: r_bad\nproject: p1\nretracted_at: 2026-11-08T00:00:00+00:00\n---\nmissing target\n", encoding="utf-8")
        store10.close()
        try:
            Store(temp / "r10")
            ok("语义损坏的撤回账启动失败", False)
        except ValueError:
            ok("语义损坏的撤回账启动失败", True)

        # Backup restore containing both primary and derived facts is replayed.
        store11 = Store(temp / "r11")
        src11 = store11.append({"event_id": "e42", "project": "p1", "subject": "user",
                                "text": "备份级联来源", "source_msg": "src-42"})["fact_id"]
        derived11 = store11.create_candidate({"project": "p1", "subject": "user",
                                              "text": "备份级联派生", "source_event_id": "src-42"})["candidate"]
        derived11_fact = store11.approve_candidate(derived11["id"], derived11["content_hash"])["fact_id"]
        backup11 = temp / "memory-backup-r11"
        shutil.copytree(store11.memory, backup11)
        store11.retract(src11, "forget both")
        store11.close()
        shutil.rmtree(temp / "r11" / "memory")
        shutil.copytree(backup11, temp / "r11" / "memory")
        restored11 = Store(temp / "r11")
        ok("含主+派生的旧备份恢复后级联重放", restored11.query("p1") == []
           and restored11.query("p1", mode="all") == []
           and sum(1 for fid in (src11, derived11_fact)
                   if restored11.get_fact(fid)["status"] == "retracted") == 2)
        restored11.close()

        # Fail-closed: unrecoverable retraction must stop serving reads in-process.
        store12 = Store(temp / "r12")
        f12 = store12.append({"event_id": "e50", "project": "p1", "subject": "user",
                              "text": "毒化事实", "source_msg": "src-50"})["fact_id"]
        store12._close_fact = lambda *a, **k: False
        try:
            store12.retract(f12, "forget")
        except RuntimeError:
            pass
        ok("持续失败后读取被封锁", store12.read_failure is not None)
        try:
            store12.query("p1")
            ok("封锁后 query 显式报错", False)
        except RuntimeError:
            ok("封锁后 query 显式报错", True)
        store12.close()

        # Mirror outside the store root survives a whole-root rollback.
        mirror = temp / "mirror-r13"
        store13 = Store(temp / "r13", retraction_mirror=mirror)
        f13 = store13.append({"event_id": "e51", "project": "p1", "subject": "user",
                              "text": "镜像事实", "source_msg": "src-51"})["fact_id"]
        whole_backup = temp / "root-backup-r13"
        shutil.copytree(temp / "r13", whole_backup)
        store13.retract(f13, "forget with mirror")
        store13.close()
        shutil.rmtree(temp / "r13")
        shutil.copytree(whole_backup, temp / "r13")
        restored13 = Store(temp / "r13", retraction_mirror=mirror)
        ok("整根目录回滚后镜像账重放生效", restored13.get_fact(f13)["status"] == "retracted"
           and restored13.query("p1") == [])
        restored13.close()

        # Strict ledger: a missing list key is corruption, not an empty cascade.
        store14 = Store(temp / "r14")
        bad14 = temp / "r14" / "retractions" / "p1" / "r_missing.md"
        bad14.parent.mkdir(parents=True, exist_ok=True)
        bad14.write_text("---\nid: r_missing\nproject: p1\ntarget_fact_id: f_x\nretracted_at: 2026-11-08T00:00:00+00:00\n---\n", encoding="utf-8")
        store14.close()
        try:
            Store(temp / "r14")
            ok("账本缺字段启动失败", False)
        except ValueError:
            ok("账本缺字段启动失败", True)

        # Cross-project target is corruption.
        store15 = Store(temp / "r15")
        other_project_fact = store15.append({"event_id": "e52", "project": "p2", "subject": "user",
                                             "text": "他项目事实"})["fact_id"]
        bad15 = temp / "r15" / "retractions" / "p1" / "r_cross.md"
        bad15.parent.mkdir(parents=True, exist_ok=True)
        bad15.write_text("---\nid: r_cross\nproject: p1\ntarget_fact_id: " + other_project_fact
                         + "\ntarget_source_ids: []\ncascade_candidate_ids: []\ncascade_fact_ids: []\n"
                         + "retracted_at: 2026-11-08T00:00:00+00:00\n---\n", encoding="utf-8")
        store15.close()
        try:
            Store(temp / "r15")
            ok("跨项目目标账本启动失败", False)
        except ValueError:
            ok("跨项目目标账本启动失败", True)

        # Mirror degradation: retraction still takes effect, then surfaces the error.
        mirror_file = temp / "mirror-blocked"
        store16 = Store(temp / "r16", retraction_mirror=mirror_file)
        f16 = store16.append({"event_id": "e60", "project": "p1", "subject": "user",
                              "text": "镜像降级事实", "source_msg": "src-60"})["fact_id"]
        shutil.rmtree(mirror_file / "retractions")
        (mirror_file / "retractions").write_text("occupied", encoding="utf-8")
        raised16 = False
        try:
            store16.retract(f16, "forget")
        except RuntimeError:
            raised16 = True
        ok("镜像写失败：撤回已生效但显式报错", raised16
           and store16.get_fact(f16)["status"] == "retracted")
        (mirror_file / "retractions").unlink()
        (mirror_file / "retractions").mkdir()
        store16.retract(f16, "retry mirror")
        mirror_records = list((mirror_file / "retractions").rglob("*.md"))
        ok("重试补写镜像账", len(mirror_records) == 1)
        store16.close()

        # Startup replay must raise when a close returns False.
        store17 = Store(temp / "r17")
        f17 = store17.append({"event_id": "e61", "project": "p1", "subject": "user",
                              "text": "重放失败事实", "source_msg": "src-61"})["fact_id"]
        store17.retract(f17, "forget")
        store17.close()
        store18 = Store(temp / "r17")
        store18._close_fact = lambda *a, **k: False
        store18.db.execute("UPDATE facts SET status='current' WHERE id=?", (f17,))
        store18.db.commit()
        try:
            store18._replay_retractions()
            ok("启动重放关闭失败即报错", False)
        except RuntimeError:
            ok("启动重放关闭失败即报错", True)
        store18.close()

        # Ledger list must decode to a JSON array, not a string.
        store19 = Store(temp / "r19")
        bad19 = temp / "r19" / "retractions" / "p1" / "r_string.md"
        bad19.parent.mkdir(parents=True, exist_ok=True)
        bad19.write_text("---\nid: r_s\nproject: p1\ntarget_fact_id: f_x\ntarget_source_ids: \"abc\"\n"
                         + "cascade_candidate_ids: []\ncascade_fact_ids: []\n---\n", encoding="utf-8")
        store19.close()
        try:
            Store(temp / "r19")
            ok("账本列表非数组启动失败", False)
        except ValueError:
            ok("账本列表非数组启动失败", True)

        # Bridge plumbs the mirror through to the store.
        mirror20 = temp / "mirror-r20"
        service20 = BridgeService(temp / "r20", port=0, retraction_mirror=mirror20)
        try:
            ok("桥传递撤回镜像", service20.store.mirror is not None
               and str(service20.store.mirror) == str(mirror20.resolve()))
        finally:
            service20.close()

        print(f"\n结果: {sum(checks)}/{len(checks)} 通过")
        if len(checks) != sum(checks):
            raise SystemExit(1)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
