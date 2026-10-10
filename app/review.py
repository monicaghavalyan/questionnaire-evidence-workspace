"""Reviewer actions and the approved-answer lifecycle (rule 3).

    draft        -> a model proposal, or a human edit that nobody has approved
    approved     -> a human signed off; only this state may be reused
    needs_review -> was approved, but a document it cites has changed version
    unresolved   -> no checkable evidence; parked with a topic owner

Approval takes a snapshot of the version of every document the answer cites. That snapshot
is what makes the source-change check possible later: compare it with the current versions
and any difference means the evidence underneath the approval has moved.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from app import storage

REUSABLE_STATUS = "approved"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def snapshot_source_versions(
    conn: sqlite3.Connection, cited_passage_ids: list[str]
) -> dict[str, int]:
    """Version of each document behind the cited passages, at this moment."""
    snapshot: dict[str, int] = {}
    for pid in cited_passage_ids:
        doc = storage.passage_owner_document(conn, pid)
        if doc is not None:
            snapshot[doc["id"]] = int(doc["version"])
    return snapshot


def source_versions_changed(
    conn: sqlite3.Connection, snapshot: dict[str, int]
) -> dict[str, dict[str, int | None]]:
    """Compare an approval snapshot against the live document versions."""
    current = storage.document_versions(conn)
    changed: dict[str, dict[str, int | None]] = {}
    for doc_id, version in (snapshot or {}).items():
        live = current.get(doc_id)
        if live is None or int(live) != int(version):
            changed[doc_id] = {"approved_with_version": int(version), "current_version": live}
    return changed


def cited_authority_lost(
    conn: sqlite3.Connection, cited_passage_ids: list[str]
) -> list[dict[str, str]]:
    """Cited passages whose document has since been superseded by a newer one.

    Found while testing the --mode supersede path: inserting EXPORT-v3 leaves EXPORT-v2 at
    version 2, so the version comparison alone saw no change, yet the approved answer was
    now resting on a superseded passage. Rule 2 forbids answering from one, so an approval
    in that position has to go back for review too.
    """
    lost: list[dict[str, str]] = []
    for pid in cited_passage_ids or []:
        doc = storage.passage_owner_document(conn, pid)
        if doc is None:
            lost.append({"passage_id": pid, "problem": "passage no longer exists"})
        elif not doc["is_authoritative"]:
            lost.append(
                {
                    "passage_id": pid,
                    "problem": f"{doc['id']} is now superseded by {doc['superseded_by']}",
                }
            )
    return lost


def approval_is_stale(
    conn: sqlite3.Connection, answer: dict[str, Any]
) -> dict[str, Any] | None:
    """Both staleness reasons in one place: version moved, or evidence lost authority."""
    changed = source_versions_changed(conn, answer.get("source_versions") or {})
    lost = cited_authority_lost(conn, answer.get("cited_passage_ids") or [])
    if not changed and not lost:
        return None
    return {"version_changed": changed, "authority_lost": lost}


def edit(
    conn: sqlite3.Connection, question_id: str, answer_text: str, note: str | None = None
) -> dict[str, Any]:
    """Record a reviewer's wording. An edit alone is a draft - rule 3 is explicit."""
    answer = storage.get_answer(conn, question_id)
    if answer is None:
        raise ValueError(f"no answer row for {question_id}")

    previous = answer.get("answer_text")
    timestamp = now()
    storage.upsert_answer(
        conn,
        {
            "question_id": question_id,
            "answer_text": answer_text,
            "status": "draft",
            "origin": "human_edit",
            "reviewer_note": note,
            # An edit withdraws any earlier approval: the approved wording no longer
            # matches what is stored, so it must be re-approved before reuse.
            "approved_at": None,
            "approved_by": None,
            "source_versions": {},
            "reused_from": None,
            "updated_at": timestamp,
        },
    )
    storage.log_event(
        conn,
        question_id,
        "edited",
        {"from": previous, "to": answer_text, "note": note},
        timestamp,
    )
    return storage.get_answer(conn, question_id)


def approve(
    conn: sqlite3.Connection, question_id: str, approved_by: str = "reviewer"
) -> dict[str, Any]:
    """Approve the stored wording and freeze the versions of its sources."""
    answer = storage.get_answer(conn, question_id)
    if answer is None:
        raise ValueError(f"no answer row for {question_id}")
    if not (answer.get("answer_text") or "").strip():
        raise ValueError(f"{question_id} has no answer text to approve")
    if answer["status"] == "unresolved":
        raise ValueError(
            f"{question_id} is unresolved: give it an answer before approving, so an "
            "approval is never recorded against evidence that failed its checks"
        )
    lost = cited_authority_lost(conn, answer.get("cited_passage_ids") or [])
    if lost:
        raise ValueError(
            f"{question_id} cites evidence that is no longer authoritative "
            f"({'; '.join(item['problem'] for item in lost)}). Re-draft it from the current "
            "documents before approving."
        )

    snapshot = snapshot_source_versions(conn, answer.get("cited_passage_ids") or [])
    timestamp = now()
    storage.upsert_answer(
        conn,
        {
            "question_id": question_id,
            "status": "approved",
            "source_versions": snapshot,
            # The reviewer has looked again and the snapshot is fresh, so an earlier
            # source-change warning no longer describes this approval.
            "warnings": [
                w for w in answer.get("warnings") or [] if w["code"] != "source_version_changed"
            ],
            "approved_at": timestamp,
            "approved_by": approved_by,
            "updated_at": timestamp,
        },
    )
    storage.log_event(
        conn,
        question_id,
        "approved",
        {"by": approved_by, "source_versions": snapshot,
         "answer_text": answer.get("answer_text")},
        timestamp,
    )
    return storage.get_answer(conn, question_id)


def park_unresolved(
    conn: sqlite3.Connection, question_id: str, note: str
) -> dict[str, Any]:
    """Leave a question unresolved with a reviewer's note, keeping the assigned owner."""
    answer = storage.get_answer(conn, question_id)
    if answer is None:
        raise ValueError(f"no answer row for {question_id}")
    timestamp = now()
    storage.upsert_answer(
        conn,
        {
            "question_id": question_id,
            "status": "unresolved",
            "reviewer_note": note,
            "approved_at": None,
            "approved_by": None,
            "source_versions": {},
            "updated_at": timestamp,
        },
    )
    storage.log_event(conn, question_id, "parked_unresolved", {"note": note}, timestamp)
    return storage.get_answer(conn, question_id)


def refresh_source_change_flags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Flip approved answers to needs_review when a cited document's version has moved.

    Called on every queue load and before any reuse, so a stale approval cannot be served
    just because nobody happened to open that row.
    """
    flagged: list[dict[str, Any]] = []
    for answer in storage.all_answers(conn):
        if answer["status"] != "approved":
            continue
        stale = approval_is_stale(conn, answer)
        if stale is None:
            continue
        changed = stale["version_changed"]
        lost = stale["authority_lost"]
        timestamp = now()
        warnings = [
            w for w in answer.get("warnings") or [] if w["code"] != "source_version_changed"
        ]
        details = []
        if changed:
            details.append(
                "version changed: "
                + "; ".join(
                    f"{doc} approved at v{info['approved_with_version']}, "
                    f"now v{info['current_version']}"
                    for doc, info in changed.items()
                )
            )
        if lost:
            details.append(
                "evidence lost authority: "
                + "; ".join(f"{item['passage_id']} - {item['problem']}" for item in lost)
            )
        warnings.append(
            {
                "code": "source_version_changed",
                "message": (
                    "A document behind this approved answer has moved ("
                    + " | ".join(details)
                    + "). The answer needs review before it can be reused."
                ),
                "passage_ids": answer.get("cited_passage_ids") or [],
            }
        )
        storage.upsert_answer(
            conn,
            {
                "question_id": answer["question_id"],
                "status": "needs_review",
                "warnings": warnings,
                "updated_at": timestamp,
            },
        )
        storage.log_event(
            conn, answer["question_id"], "flagged_needs_review", stale, timestamp
        )
        flagged.append({"question_id": answer["question_id"], **stale})
    return flagged


def find_reusable(
    conn: sqlite3.Connection, question_text: str, exclude_question_id: str | None = None
) -> dict[str, Any] | None:
    """An approved answer for the identical question text, still backed by its sources.

    Exact text matching. needs_review and draft rows are never returned:
    rule 3 permits reuse of approved answers only.
    """
    refresh_source_change_flags(conn)
    candidate = storage.find_approved_by_text(conn, question_text, exclude_question_id)
    if candidate is None:
        return None
    if approval_is_stale(conn, candidate) is not None:
        return None
    return candidate
