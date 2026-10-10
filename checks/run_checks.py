"""Run the five reference cases and report expected versus observed.

Expected values come from data/reference-cases.json, which was written by hand from the
passages. This script never writes to that file, and the application's own output is never
promoted into it: if a check fails, either the application or the hand derivation is wrong,
and that has to be decided by a person.

    python checks/run_checks.py                     # default MODEL_MODE (replay)
    MODEL_MODE=stub python checks/run_checks.py      # offline, no credentials
    MODEL_MODE=live python checks/run_checks.py      # real calls, recorded to cache/
    python checks/run_checks.py --json report.json   # machine-readable report

Each run starts from a throwaway database so results do not depend on run order.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, loader, model_client, pipeline, review, storage  # noqa: E402
from app.config import REFERENCE_CASES  # noqa: E402

REVIEWER_WORDING = "No. CSV export is limited to paid plans."


# --- comparison helpers -------------------------------------------------------

class Result:
    def __init__(self, case_id: str, title: str, covers: str, rule: str) -> None:
        self.case_id = case_id
        self.title = title
        self.covers = covers
        self.rule = rule
        self.assertions: list[dict[str, Any]] = []
        self.error: str | None = None

    def check(self, name: str, expected: Any, observed: Any, ok: bool | None = None) -> None:
        passed = (expected == observed) if ok is None else bool(ok)
        self.assertions.append(
            {"assertion": name, "expected": expected, "observed": observed, "passed": passed}
        )

    @property
    def passed(self) -> bool:
        return self.error is None and all(a["passed"] for a in self.assertions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "title": self.title,
            "covers": self.covers,
            "rule": self.rule,
            "passed": self.passed,
            "error": self.error,
            "assertions": self.assertions,
        }


def _contains_all(haystack: str, needles: list[str]) -> tuple[bool, list[str]]:
    low = (haystack or "").casefold()
    missing = [n for n in needles if n.casefold() not in low]
    return not missing, missing


def _contains_none(haystack: str, needles: list[str]) -> tuple[bool, list[str]]:
    low = (haystack or "").casefold()
    present = [n for n in needles if n.casefold() in low]
    return not present, present


# --- fresh environment per case ----------------------------------------------

def fresh_db(tmpdir: Path, name: str):
    db = tmpdir / f"{name}.sqlite"
    if db.exists():
        db.unlink()
    conn = storage.connect(db)
    loader.load(conn, reset=True)
    return conn, db


# --- single-question cases ----------------------------------------------------

def run_single_question(case: dict[str, Any], mode: str, tmpdir: Path) -> Result:
    res = Result(case["id"], case["title"], case["covers"], case["rule"])
    conn, _ = fresh_db(tmpdir, case["id"])
    try:
        answer = pipeline.draft_question(conn, case["question_id"], mode=mode)
        exp = case["expect"]

        res.check("question text matches the fixture",
                  case["question_text"], answer["question_text"])

        if exp["status"] == "answered":
            # The pipeline stores a validated answer awaiting review as 'draft'.
            res.check("status is answered and awaiting review", "draft", answer["status"])
        else:
            res.check("status", exp["status"], answer["status"])

        if "cited_passage_ids" in exp:
            res.check("cited passage ids",
                      sorted(exp["cited_passage_ids"]), sorted(answer["cited_passage_ids"]))
        if "conflicting_passage_ids" in exp:
            res.check("conflicting passage ids",
                      sorted(exp["conflicting_passage_ids"]),
                      sorted(answer["conflicting_passage_ids"]))
        if "owner" in exp:
            res.check("owner", exp["owner"], answer["owner"])

        if "answer_must_contain" in exp:
            ok, missing = _contains_all(answer["answer_text"] or "", exp["answer_must_contain"])
            res.check("answer mentions the required facts",
                      f"contains {exp['answer_must_contain']}",
                      f"missing {missing}" if missing else "all present", ok)
        if "answer_must_not_contain" in exp:
            ok, present = _contains_none(
                answer["answer_text"] or "", exp["answer_must_not_contain"]
            )
            res.check("answer invents no capability",
                      f"none of {exp['answer_must_not_contain']}",
                      f"found {present}" if present else "none present", ok)
        if "reason_must_contain" in exp:
            ok, missing = _contains_all(answer["reason"] or "", exp["reason_must_contain"])
            res.check("reason explains why it is unresolved",
                      f"contains {exp['reason_must_contain']}",
                      f"missing {missing}" if missing else "all present", ok)

        checks = answer.get("checks") or {}
        for flag in ("all_citations_exist", "all_citations_current", "all_excerpts_verified"):
            if flag in exp:
                res.check(flag.replace("_", " "), exp[flag], checks.get(flag))
    except Exception as exc:  # surfaced in the report, never silently swallowed
        res.error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return res


# --- RC-4 approval and reuse --------------------------------------------------

def run_rc4(case: dict[str, Any], mode: str, tmpdir: Path) -> Result:
    res = Result(case["id"], case["title"], case["covers"], case["rule"])
    conn, _ = fresh_db(tmpdir, case["id"])
    exp = case["expect"]
    try:
        q1 = pipeline.draft_question(conn, "Q1", mode=mode)
        res.check("Q1 drafts with evidence", "draft", q1["status"])

        review.edit(conn, "Q1", REVIEWER_WORDING, note="Tightened for buyer-facing use.")
        after_edit = storage.get_answer(conn, "Q1")
        res.check("an edit alone is only a draft", "draft", after_edit["status"])
        res.check("an unapproved edit records no approval", None, after_edit["approved_at"])

        review.approve(conn, "Q1", approved_by="reviewer")
        approved = storage.get_answer(conn, "Q1")
        res.check("Q1 is approved", "approved", approved["status"])
        res.check("approval snapshots its source versions",
                  {"EXPORT-v2": 2}, approved["source_versions"])

        q13 = pipeline.draft_question(conn, "Q13", mode=mode)
        res.check("reused answer text", exp["reused_answer_text"], q13["answer_text"])
        res.check("reused origin", exp["reused_origin"], q13["origin"])
        res.check("reused status", exp["reused_status"], q13["status"])
        res.check("reused sources", sorted(exp["reused_sources"]),
                  sorted(q13["cited_passage_ids"]))
        res.check("reused answer carries the approval record",
                  exp["reused_has_approved_at"], bool(q13["approved_at"]))
        res.check("reuse is attributed to Q1", "Q1", q13["reused_from"])

        # An unapproved edit must not become reusable knowledge.
        pipeline.draft_question(conn, "Q9", mode=mode)
        review.edit(conn, "Q9", "Deleted projects are kept for 30 days.")
        q9 = storage.get_answer(conn, "Q9")
        res.check("unapproved edit status", exp["unapproved_edit_status"], q9["status"])
        reusable = review.find_reusable(conn, q9["question_text"], exclude_question_id="Q9")
        res.check("unapproved edit is not offered for reuse",
                  exp["unapproved_edit_not_reused"], reusable is None)

        counts = pipeline.queue_summary(conn)
        res.check("a reuse is not counted as new answered work",
                  True, "Q13" in counts["reused_question_ids"])
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return res


# --- RC-5 source change -------------------------------------------------------

def run_rc5(case: dict[str, Any], mode: str, tmpdir: Path) -> Result:
    res = Result(case["id"], case["title"], case["covers"], case["rule"])
    conn, db_path = fresh_db(tmpdir, case["id"])
    exp = case["expect"]
    try:
        pipeline.draft_question(conn, "Q1", mode=mode)
        review.edit(conn, "Q1", REVIEWER_WORDING)
        review.approve(conn, "Q1")

        # Control: an approved answer whose sources are left alone.
        pipeline.draft_question(conn, "Q12", mode=mode)
        review.approve(conn, "Q12")
        res.check("control answer starts approved", "approved",
                  storage.get_answer(conn, "Q12")["status"])

        storage.update_document_version(conn, "EXPORT-v2", 3)
        flagged = review.refresh_source_change_flags(conn)
        res.check("the changed source is detected", ["Q1"], [f["question_id"] for f in flagged])

        q1 = storage.get_answer(conn, "Q1")
        res.check("Q1 status after the version change",
                  exp["q1_status_after_bump"], q1["status"])
        res.check("the stored warning names the version change", True,
                  any(w["code"] == "source_version_changed" for w in q1["warnings"]))

        reusable = review.find_reusable(conn, q1["question_text"], exclude_question_id="Q1")
        res.check("Q1 is no longer reusable", exp["q1_reusable_after_bump"], reusable is not None)

        q13 = pipeline.draft_question(conn, "Q13", mode=mode)
        res.check("Q13 does not receive the stale approved answer",
                  True, q13["origin"] != "reused_approved")

        res.check("control answer stays approved",
                  exp["unaffected_approved_status"], storage.get_answer(conn, "Q12")["status"])

        # Reload: a brand new connection to the same file, as a restart would do.
        conn.close()
        reopened = storage.connect(db_path)
        q1_reloaded = storage.get_answer(reopened, "Q1")
        res.check("status survives a reload",
                  exp["q1_status_survives_reload"], q1_reloaded["status"])
        res.check("evidence survives a reload", exp["q1_evidence_survives_reload"],
                  bool(q1_reloaded["excerpts"]) and bool(q1_reloaded["conflicting_passage_ids"]))
        res.check("the audit trail survives a reload", True,
                  any(e["event"] == "flagged_needs_review" for e in storage.events(reopened, "Q1")))
        reopened.close()
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        try:
            conn.close()
        except Exception:
            pass
    return res


# --- driver -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the five reference cases")
    parser.add_argument("--json", default=None, help="also write the report to this path")
    parser.add_argument("--mode", default=None, help="override MODEL_MODE for this run")
    args = parser.parse_args()

    mode = (args.mode or config.MODEL_MODE).lower()

    # Pre-flight. Without this, replay mode against an empty cache fails every case with a
    # confusing cascade ("Q12 has no answer text to approve") instead of the real reason.
    if mode == "replay" and not model_client.cache_inventory():
        print(
            f"\nMODEL_MODE=replay, but no recorded responses were found in "
            f"{config.CACHE_DIR}/.\n\n"
            "Record them once with a real model:\n"
            "    export MODEL_API_KEY=...\n"
            "    MODEL_MODE=live python -c \"from app import storage,pipeline; "
            "pipeline.draft_all(storage.connect(), force=True)\"\n\n"
            "Or run the checks offline against the keyword stub (not a real model run):\n"
            "    MODEL_MODE=stub python checks/run_checks.py\n",
            file=sys.stderr,
        )
        return 2

    with REFERENCE_CASES.open(encoding="utf-8") as fh:
        spec = json.load(fh)

    results: list[Result] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for case in spec["cases"]:
            if case["kind"] == "single_question":
                results.append(run_single_question(case, mode, tmpdir))
            elif case["id"] == "RC-4":
                results.append(run_rc4(case, mode, tmpdir))
            elif case["id"] == "RC-5":
                results.append(run_rc5(case, mode, tmpdir))

    # --- report ---------------------------------------------------------------
    width = 86
    print("=" * width)
    print("REFERENCE CASE RESULTS")
    print(f"model mode      : {mode}" + ("   *** STUB: not a real model run ***" if mode == "stub" else ""))
    print(f"model id        : {config.MODEL_ID if mode != 'stub' else 'stub-keyword-overlap'}")
    print(f"strict coverage : {config.STRICT_COVERAGE}")
    print(f"recorded responses in cache: {len(model_client.cache_inventory())}")
    print(f"run at          : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print("=" * width)

    for res in results:
        mark = "PASS" if res.passed else "FAIL"
        print(f"\n[{mark}] {res.case_id}  {res.title}")
        print(f"       covers: {res.covers}")
        print(f"       rule  : {res.rule}")
        if res.error:
            print(f"       ERROR : {res.error}")
        for a in res.assertions:
            flag = "ok  " if a["passed"] else "FAIL"
            print(f"       {flag} {a['assertion']}")
            if not a["passed"]:
                print(f"            expected: {a['expected']!r}")
                print(f"            observed: {a['observed']!r}")

    passed = sum(1 for r in results if r.passed)
    print("\n" + "=" * width)
    print(f"{passed}/{len(results)} reference cases passed")
    failed = [r.case_id for r in results if not r.passed]
    if failed:
        print(f"unresolved failures: {', '.join(failed)}")
    print("=" * width)

    report = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_mode": mode,
        "model_id": config.MODEL_ID if mode != "stub" else "stub-keyword-overlap",
        "stub_warning": (
            "Results produced with the offline stub, not a real model." if mode == "stub" else None
        ),
        "strict_coverage": config.STRICT_COVERAGE,
        "recorded_responses": len(model_client.cache_inventory()),
        "passed": passed,
        "total": len(results),
        "cases": [r.as_dict() for r in results],
    }
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        with Path(args.json).open("w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"report written to {args.json}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
