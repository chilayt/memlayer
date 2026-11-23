"""Run the forgetting self-test in an isolated temporary store."""
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memlayer"))
from memlayer import Store


def check(directory: Path) -> bool:
    ROOT = directory / "store"
    MIRROR = directory / "mirror"
    PROJ = "p_forgettest"
    store = Store(ROOT, retraction_mirror=MIRROR)

    def add(eid, subj, text, src):
        return store.append({"event_id": eid, "project": PROJ, "subject": subj,
                             "text": text, "source_msg": src, "confirmed": True})

    # facts: A,B share source s1 (cascade); C,D unrelated
    fa = add("eA", "alpha-deploy", "项目Alpha的部署密钥是SECRET-XYZ-123", "s1")
    fb = add("eB", "alpha-flow", "项目Alpha的部署流程分三步", "s1")
    fc = add("eC", "beta-arch", "项目Beta采用微服务架构", "s2")
    fd = add("eD", "code-style", "通用编码规范遵循PEP8", "s3")
    A, B, C, D = fa["fact_id"], fb["fact_id"], fc["fact_id"], fd["fact_id"]

    def recalled_ids():
        return {f["id"] for f in store.query(PROJ, mode="current")}

    results = {}
    # Suite 1: storage
    results["1_storage"] = {A,B,C,D} <= recalled_ids()
    # Suite 2: removal — retract A
    store.retract(A, "user forget request")
    rec = recalled_ids()
    results["2_removal_A_not_recalled"] = A not in rec
    # Suite 3: cascade — B (same source s1) also retracted
    results["3_cascade_B_retracted"] = B not in rec
    # Suite 4: preservation — C,D still recalled
    results["4_preserve_C"] = C in rec
    results["4_preserve_D"] = D in rec
    # Suite 5: persistence — replay retractions (simulate backup restore)
    store._replay_retractions(strict=True)
    rec2 = recalled_ids()
    results["5_persist_A_still_retracted"] = A not in rec2
    results["5_persist_B_still_retracted"] = B not in rec2
    results["5_persist_C_survives"] = C in rec2
    # Suite 6: extraction resistance — where does A's content still live?
    secret = "SECRET-XYZ-123"
    residual = {}
    # markdown fact file (tombstoned but text may remain)
    for f in (ROOT/"memory"/PROJ).glob("*.md"):
        if secret in f.read_text(encoding="utf-8"):
            residual["markdown_fact_file"] = str(f.name)
    # retraction ledger / mirror
    for f in MIRROR.rglob("*"):
        if f.is_file() and secret in f.read_text(encoding="utf-8", errors="ignore"):
            residual.setdefault("mirror", []).append(str(f.relative_to(MIRROR)))
    # sqlite index
    con = sqlite3.connect(ROOT / "index.db")
    try:
        hit = con.execute("SELECT COUNT(*) FROM facts WHERE text LIKE ?", (f"%{secret}%",)).fetchone()[0]
        if hit:
            residual["sqlite_facts"] = hit
    finally:
        con.close()
    results["6_extraction_residual"] = residual
    # Suite 6b: mode="all" leak vector (API accepts mode param)
    all_ids = {f["id"] for f in store.query(PROJ, mode="all")}
    results["6b_mode_all_leaks_retracted"] = A in all_ids
    # Suite 7: relearning — new fact from retracted source s1
    try:
        fe = add("eE", "alpha-new", "项目Alpha的新部署说明", "s1")
        rec3 = recalled_ids()
        results["7_relearn_s1_blocked"] = fe["fact_id"] not in rec3
        results["7_relearn_note"] = "appended; in current recall=" + str(fe["fact_id"] in rec3)
    except ValueError as e:
        results["7_relearn_s1_blocked"] = True
        results["7_relearn_note"] = "raised: " + str(e)[:80]
    # tombstone audit marker present?
    tomb = False
    for f in (ROOT/"memory"/PROJ).glob("*.md"):
        txt = f.read_text(encoding="utf-8")
        if "RETRACTED" in txt and secret in txt:
            tomb = True
    results["8_tombstone_audit_marker"] = tomb
    store.close()
    print(json.dumps(results, ensure_ascii=False, indent=1))

    return all(value for key, value in results.items() if isinstance(value, bool) and key != "6b_mode_all_leaks_retracted") and not results["6b_mode_all_leaks_retracted"]


if __name__ == "__main__":
    with TemporaryDirectory(prefix="memlayer-forget-test-") as directory:
        passed = check(Path(directory))
    raise SystemExit(0 if passed else 1)
