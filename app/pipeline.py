"""End-to-end flow for one question.

    reuse check -> model draft -> parse -> validate -> persist

Reuse comes first: if a reviewer has already approved an answer to this exact question and
its sources have not moved, there is no reason to spend a model call, and rule 3 says the
approved wording is what should be served.

Model and transport failures are turned into a visible "error" row rather than an exception
that stops the batch, so one bad question never hides the rest of the queue.

Draft the whole questionnaire from the command line and print what happened:

    MODEL_MODE=live python -m app.pipeline --force    # real calls, recorded to cache/
    MODEL_MODE=replay python -m app.pipeline --force  # recorded responses, no API key
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from app import config, drafting, model_client, review, storage, validation


def draft_question(
    conn: sqlite3.Connection,
    question_id: str,
    *,
    mode: str | None = None,
    allow_reuse: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    question = storage.get_question(conn, question_id)
    if question is None:
        raise ValueError(f"unknown question id {question_id!r}")

    timestamp = review.now()
    existing = storage.get_answer(conn, question_id)

    # Without force, only questions with no answer yet, or whose last attempt failed, are
    # drafted. This never overwrites reviewer work and never pays for a call whose result
    # is already stored.
    if existing and not force and existing["status"] != "error":
        return existing

    base = {
        "question_id": question_id,
        "question_text": question["text"],
        "created_at": (existing or {}).get("created_at") or timestamp,
        "updated_at": timestamp,
    }

    # --- 1. reuse an approved answer -----------------------------------------
    if allow_reuse:
        reusable = review.find_reusable(conn, question["text"], exclude_question_id=question_id)
        if reusable is not None:
            record = {
                **base,
                "answer_text": reusable["answer_text"],
                "status": "approved",
                "origin": "reused_approved",
                "cited_passage_ids": reusable.get("cited_passage_ids") or [],
                "conflicting_passage_ids": reusable.get("conflicting_passage_ids") or [],
                "excerpts": reusable.get("excerpts") or {},
                "warnings": [],
                "checks": {"reused": True, "reused_from": reusable["question_id"]},
                "owner": None,
                "reason": "",
                "source_versions": reusable.get("source_versions") or {},
                "model_mode": None,
                "model_id": None,
                "cache_key": None,
                "reviewer_note": reusable.get("reviewer_note"),
                "reused_from": reusable["question_id"],
                "approved_at": reusable.get("approved_at"),
                "approved_by": reusable.get("approved_by"),
            }
            storage.upsert_answer(conn, record)
            storage.log_event(
                conn,
                question_id,
                "reused_approved",
                {
                    "from_question": reusable["question_id"],
                    "approved_at": reusable.get("approved_at"),
                    "sources": reusable.get("cited_passage_ids"),
                },
                timestamp,
            )
            return storage.get_answer(conn, question_id)

    # --- 2. model draft -------------------------------------------------------
    system, user = drafting.prompt_for_question(conn, question["text"])
    try:
        response = model_client.complete(system, user, mode=mode)
    except model_client.ModelError as exc:
        record = {
            **base,
            "answer_text": None,
            "status": "error",
            "origin": "system",
            "cited_passage_ids": [],
            "conflicting_passage_ids": [],
            "excerpts": {},
            "warnings": [
                {"code": "model_unavailable", "message": str(exc), "passage_ids": []}
            ],
            "checks": {},
            "owner": storage.owner_for_topic(conn, question["topic"]),
            "reason": f"The model could not be reached: {exc}",
            "source_versions": {},
            "model_mode": (mode or config.MODEL_MODE),
            "model_id": None,
            "cache_key": None,
            "reused_from": None,
            "approved_at": None,
            "approved_by": None,
        }
        storage.upsert_answer(conn, record)
        storage.log_event(conn, question_id, "model_error", {"error": str(exc)}, timestamp)
        return storage.get_answer(conn, question_id)

    # --- 3. parse -------------------------------------------------------------
    try:
        parsed = drafting.parse_response(response.text)
    except drafting.ParseError as exc:
        record = {
            **base,
            "answer_text": None,
            "status": "unresolved",
            "origin": "model_draft",
            "cited_passage_ids": [],
            "conflicting_passage_ids": [],
            "excerpts": {},
            "warnings": [
                {
                    "code": "invalid_model_output",
                    "message": f"The model response could not be parsed: {exc}",
                    "passage_ids": [],
                }
            ],
            "checks": {"parsed": False},
            "owner": storage.owner_for_topic(conn, question["topic"]),
            "reason": f"Unusable model output, routed for review. {exc}",
            "source_versions": {},
            "model_mode": response.mode,
            "model_id": response.model_id,
            "cache_key": response.cache_key,
            "reused_from": None,
            "approved_at": None,
            "approved_by": None,
        }
        storage.upsert_answer(conn, record)
        storage.log_event(
            conn, question_id, "invalid_model_output", {"error": str(exc)}, timestamp
        )
        return storage.get_answer(conn, question_id)

    # --- 4. validate ----------------------------------------------------------
    outcome = validation.validate(conn, question, parsed)

    record = {
        **base,
        "answer_text": outcome["answer_text"],
        "status": outcome["status"],
        "origin": "model_draft",
        "cited_passage_ids": outcome["cited_passage_ids"],
        "conflicting_passage_ids": outcome["conflicting_passage_ids"],
        "excerpts": outcome["excerpts"],
        "warnings": outcome["warnings"],
        "checks": outcome["checks"],
        "owner": outcome["owner"],
        "reason": outcome["reason"],
        "source_versions": {},
        "model_mode": response.mode,
        "model_id": response.model_id,
        "cache_key": response.cache_key,
        "reused_from": None,
        "approved_at": None,
        "approved_by": None,
    }
    if outcome["status"] == "answered":
        record["status"] = "draft"

    storage.upsert_answer(conn, record)
    storage.log_event(
        conn,
        question_id,
        "drafted",
        {
            "model_mode": response.mode,
            "model_id": response.model_id,
            "cache_key": response.cache_key,
            "validated_status": outcome["status"],
            "model_proposed": outcome["model_proposed"],
            "blocking_warnings": outcome["checks"].get("blocking_warnings", []),
        },
        timestamp,
    )
    return storage.get_answer(conn, question_id)


def draft_all(
    conn: sqlite3.Connection, *, mode: str | None = None, force: bool = False
) -> list[dict[str, Any]]:
    review.refresh_source_change_flags(conn)
    results = []
    for question in storage.all_questions(conn):
        results.append(draft_question(conn, question["id"], mode=mode, force=force))
    return results


def queue_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the operations view. Reuses are not counted as new answered work."""
    review.refresh_source_change_flags(conn)
    answers = storage.all_answers(conn)
    counts = {"draft": 0, "approved": 0, "needs_review": 0, "unresolved": 0, "error": 0}
    for a in answers:
        counts[a["status"]] = counts.get(a["status"], 0) + 1

    reused = [a["question_id"] for a in answers if a["origin"] == "reused_approved"]
    by_owner: dict[str, int] = {}
    for a in answers:
        if a["status"] in ("unresolved", "error") and a.get("owner"):
            by_owner[a["owner"]] = by_owner.get(a["owner"], 0) + 1

    warning_counts: dict[str, int] = {}
    for a in answers:
        for w in a.get("warnings") or []:
            warning_counts[w["code"]] = warning_counts.get(w["code"], 0) + 1

    return {
        "total_questions": len(storage.all_questions(conn)),
        "answers": len(answers),
        "counts": counts,
        "answered_excluding_reuse": sum(
            1 for a in answers if a["status"] in ("draft", "approved")
            and a["origin"] != "reused_approved"
        ),
        "reused_question_ids": reused,
        "unresolved_by_owner": by_owner,
        "warning_counts": warning_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Draft every question and print a summary")
    parser.add_argument(
        "--force", action="store_true", help="re-draft rows that already have an answer"
    )
    args = parser.parse_args()

    conn = storage.connect()
    results = draft_all(conn, force=args.force)
    summary = queue_summary(conn)
    print(f"model mode: {config.MODEL_MODE}   model id: {config.MODEL_ID}")
    print(json.dumps(summary["counts"], indent=2))

    # Make a run where every call failed obvious from the exit code and the output.
    errors = [r for r in results if r["status"] == "error"]
    for r in errors:
        print(f"{r['question_id']}: {r['reason']}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
