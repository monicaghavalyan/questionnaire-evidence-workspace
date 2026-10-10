"""Failure-handling checks, using SIMULATED model responses.

Checks that invalid model output, missing references and API failures are handled visibly.
Simulated failures are labelled so they cannot be mistaken for real model calls. Every response in this file is hand-written and injected in place of the model; none
of them came from a provider, and none of them is written to cache/.

    python checks/run_failure_checks.py
    python checks/run_failure_checks.py --json checks/report-failures.json

Each case runs against a throwaway database. Questions that are not being failed on purpose
are served from the recorded real responses in cache/ (replay), so the batch case also shows
that one failure does not stop the rest of the queue.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import loader, model_client, pipeline, storage  # noqa: E402

SIMULATED_MODEL_ID = "simulated-failure (hand-written, not a model)"

_real_complete = model_client.complete


def _simulated(text: str) -> model_client.ModelResponse:
    return model_client.ModelResponse(
        text=text, mode="simulated", model_id=SIMULATED_MODEL_ID,
        cache_key="simulated", cached=False,
    )


def _inject(question_text: str, behaviour: Callable[[], model_client.ModelResponse]):
    """Replace the model for one question only; every other question replays real responses."""

    def fake_complete(system: str, user: str, **kwargs: Any) -> model_client.ModelResponse:
        if f"QUESTION: {question_text}\n" in user:
            return behaviour()
        kwargs["mode"] = "replay"
        return _real_complete(system, user, **kwargs)

    return fake_complete


def _raise_unavailable() -> model_client.ModelResponse:
    raise model_client.ModelError("SIMULATED: provider unavailable (HTTP 529 overloaded)")


CASES: list[dict[str, Any]] = [
    {
        "id": "F-1",
        "title": "API failure on one question does not stop the batch",
        "question_id": "Q3",
        "behaviour": _raise_unavailable,
        "batch": True,
        "expect_status": "error",
        "expect_warning": "model_unavailable",
    },
    {
        "id": "F-2",
        "title": "Output that is not JSON is routed for review, not guessed at",
        "question_id": "Q5",
        "behaviour": lambda: _simulated("Sure! Users sign in with email and password."),
        "expect_status": "unresolved",
        "expect_warning": "invalid_model_output",
    },
    {
        "id": "F-3",
        "title": "A citation to a passage that does not exist is rejected",
        "question_id": "Q5",
        "behaviour": lambda: _simulated(json.dumps({
            "status": "answered",
            "answer": "Users sign in with single sign-on.",
            "cited_passage_ids": ["ACCESS-v9:p1"],
            "quoted_excerpts": {"ACCESS-v9:p1": "Users sign in with single sign-on."},
            "conflicting_passage_ids": [],
            "reason": "",
        })),
        "expect_status": "unresolved",
        "expect_warning": "citation_not_found",
    },
    {
        "id": "F-4",
        "title": "A paraphrased 'quotation' is rejected",
        "question_id": "Q5",
        "behaviour": lambda: _simulated(json.dumps({
            "status": "answered",
            "answer": "Users log in using their email address and a password.",
            "cited_passage_ids": ["ACCESS-v1:p1"],
            "quoted_excerpts": {"ACCESS-v1:p1": "Users log in using their email and a password."},
            "conflicting_passage_ids": [],
            "reason": "",
        })),
        "expect_status": "unresolved",
        "expect_warning": "excerpt_not_verbatim",
    },
    {
        "id": "F-5",
        "title": "An answer built on a superseded passage is rejected",
        "question_id": "Q1",
        "behaviour": lambda: _simulated(json.dumps({
            "status": "answered",
            "answer": "Yes, CSV exports are available on every plan.",
            "cited_passage_ids": ["EXPORT-v1:p1"],
            "quoted_excerpts": {"EXPORT-v1:p1": "CSV exports are available on every plan."},
            "conflicting_passage_ids": [],
            "reason": "",
        })),
        "expect_status": "unresolved",
        "expect_warning": "citation_superseded",
    },
    {
        "id": "F-6",
        "title": "A conflict between two current documents is left for review",
        "question_id": "Q3",
        "behaviour": lambda: _simulated(json.dumps({
            "status": "answered",
            "answer": "Email support is available Monday to Friday, 09:00 to 17:00 UTC.",
            "cited_passage_ids": ["SUPPORT-v1:p1"],
            "quoted_excerpts": {
                "SUPPORT-v1:p1": "Email support is available Monday to Friday, 09:00 to 17:00 UTC."
            },
            # ACCESS-v1 is current: nothing supersedes it, so metadata cannot settle this.
            "conflicting_passage_ids": ["ACCESS-v1:p1"],
            "reason": "",
        })),
        "expect_status": "unresolved",
        "expect_warning": "unresolved_conflict",
    },
]


def run_case(case: dict[str, Any], tmpdir: Path) -> dict[str, Any]:
    conn = storage.connect(tmpdir / f"{case['id']}.sqlite")
    loader.load(conn, reset=True)
    question = storage.get_question(conn, case["question_id"])

    model_client.complete = _inject(question["text"], case["behaviour"])
    try:
        if case.get("batch"):
            pipeline.draft_all(conn)
        else:
            pipeline.draft_question(conn, case["question_id"])
    finally:
        model_client.complete = _real_complete

    answer = storage.get_answer(conn, case["question_id"])
    codes = [w["code"] for w in answer.get("warnings") or []]
    assertions = [
        ("status", case["expect_status"], answer["status"]),
        ("warning raised", case["expect_warning"], case["expect_warning"] if case["expect_warning"] in codes else codes),
        ("no answer text presented", "", answer["answer_text"] or ""),
    ]
    if case.get("batch"):
        others = [a for a in storage.all_answers(conn) if a["question_id"] != case["question_id"]]
        errored = [a["question_id"] for a in others if a["status"] == "error"]
        assertions.append(("other questions still processed", len(storage.all_questions(conn)) - 1, len(others)))
        assertions.append(("no other question errored", [], errored))

    checks = [
        {"assertion": name, "expected": exp, "observed": obs, "passed": exp == obs}
        for name, exp, obs in assertions
    ]
    return {
        "case": case["id"],
        "title": case["title"],
        "question_id": case["question_id"],
        "simulated": True,
        "passed": all(c["passed"] for c in checks),
        "assertions": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the simulated failure-handling checks")
    parser.add_argument("--json", default=None, help="also write the report to this path")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        results = [run_case(case, Path(tmp)) for case in CASES]

    width = 86
    print("=" * width)
    print("FAILURE-HANDLING CHECKS")
    print("*** SIMULATED: the failing responses below are hand-written, not model output ***")
    print("other questions : replayed from recorded real responses in cache/")
    print(f"run at          : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print("=" * width)
    for res in results:
        print(f"\n[{'PASS' if res['passed'] else 'FAIL'}] {res['case']}  {res['title']}  ({res['question_id']})")
        for a in res["assertions"]:
            print(f"       {'ok  ' if a['passed'] else 'FAIL'} {a['assertion']}")
            if not a["passed"]:
                print(f"            expected: {a['expected']!r}")
                print(f"            observed: {a['observed']!r}")

    passed = sum(1 for r in results if r["passed"])
    print("\n" + "=" * width)
    print(f"{passed}/{len(results)} failure-handling checks passed")
    print("=" * width)

    if args.json:
        with Path(args.json).open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "simulated": True,
                    "note": "Failing responses are hand-written and injected; they are not model output.",
                    "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "passed": passed,
                    "total": len(results),
                    "cases": results,
                },
                fh, indent=2, ensure_ascii=False,
            )
        print(f"report written to {args.json}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
