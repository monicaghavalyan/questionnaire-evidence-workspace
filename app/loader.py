"""Load the evidence corpus and questionnaire into SQLite, resolving authority in code.

Rule 2 of domain.md: "A document may replace another only through its explicit supersedes
field. A newer date alone does not establish authority."

That is deterministic, so it is decided here and never delegated to the model. The model
is later shown each passage already labelled authoritative or superseded.

Run:  python -m app.loader --reset
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from app import storage
from app.config import ADDITIONS, STARTER_SEED


class LoaderError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _strip_comments(obj: Any) -> Any:
    """Remove the _comment / _why annotation keys used in the fixture files."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def merge_fixtures(
    seed_path: Path = STARTER_SEED, additions_path: Path | None = ADDITIONS
) -> dict[str, Any]:
    """Starter seed first, candidate additions merged on top. Seed file is never modified."""
    seed = _strip_comments(_read_json(seed_path))
    merged: dict[str, Any] = {
        "documents": [dict(d, _source=str(seed_path.name)) for d in seed.get("documents", [])],
        "questions": [dict(q, _source=str(seed_path.name)) for q in seed.get("questions", [])],
        "owners": dict(seed.get("owners", {})),
    }

    if additions_path and Path(additions_path).exists():
        add = _strip_comments(_read_json(Path(additions_path)))
        name = Path(additions_path).name
        merged["documents"] += [dict(d, _source=name) for d in add.get("documents", [])]
        merged["questions"] += [dict(q, _source=name) for q in add.get("questions", [])]
        merged["owners"].update(add.get("owners", {}))

    return merged


def resolve_authority(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each document authoritative or not, using only status and supersedes.

    A document is authoritative when it is not explicitly superseded by another document.
    Dates are loaded for display but take no part in this decision.
    """
    by_id = {d["id"]: d for d in documents}

    superseded_by: dict[str, str] = {}
    for doc in documents:
        target = doc.get("supersedes")
        if target is None:
            continue
        if target not in by_id:
            raise LoaderError(
                f"{doc['id']} declares supersedes={target!r}, which is not a loaded document"
            )
        if target in superseded_by:
            raise LoaderError(
                f"{target} is superseded by both {superseded_by[target]} and {doc['id']}; "
                "the supplied rules do not say which wins"
            )
        superseded_by[target] = doc["id"]

    resolved = []
    for doc in documents:
        replacement = superseded_by.get(doc["id"])
        out = dict(doc)
        out["superseded_by"] = replacement
        out["is_authoritative"] = replacement is None

        # Cross-check the declared status against what the supersedes graph implies.
        # A mismatch is reported rather than silently corrected: the graph decides,
        # and the discrepancy is surfaced so a reviewer can see it.
        declared_current = doc.get("status") == "current"
        if declared_current != out["is_authoritative"]:
            out["status_discrepancy"] = (
                f"declared status={doc.get('status')!r} but supersedes graph says "
                f"{'authoritative' if out['is_authoritative'] else 'superseded by ' + str(replacement)}"
            )
        resolved.append(out)
    return resolved


def load(
    conn: sqlite3.Connection,
    seed_path: Path = STARTER_SEED,
    additions_path: Path | None = ADDITIONS,
    reset: bool = False,
) -> dict[str, Any]:
    if reset:
        storage.reset(conn)

    fixtures = merge_fixtures(seed_path, additions_path)
    documents = resolve_authority(fixtures["documents"])

    report: dict[str, Any] = {
        "documents": 0,
        "passages": 0,
        "questions": 0,
        "owners": 0,
        "authoritative": [],
        "superseded": [],
        "discrepancies": [],
        "duplicate_passage_ids": [],
        "missing_owner_topics": [],
    }

    seen_passages: set[str] = set()
    for doc in documents:
        storage.insert_document(conn, doc, doc.get("_source", "unknown"))
        report["documents"] += 1
        (report["authoritative"] if doc["is_authoritative"] else report["superseded"]).append(
            doc["id"]
        )
        if "status_discrepancy" in doc:
            report["discrepancies"].append({doc["id"]: doc["status_discrepancy"]})

        for passage in doc.get("passages", []):
            pid = passage["id"]
            if pid in seen_passages:
                report["duplicate_passage_ids"].append(pid)
                continue
            seen_passages.add(pid)
            if not pid.startswith(f"{doc['id']}:"):
                report["discrepancies"].append(
                    {pid: f"passage id does not carry its document prefix {doc['id']}:"}
                )
            storage.insert_passage(conn, pid, doc["id"], passage["text"])
            report["passages"] += 1

    for topic, owner in fixtures["owners"].items():
        storage.set_owner(conn, topic, owner)
        report["owners"] += 1

    seen_questions: set[str] = set()
    for q in fixtures["questions"]:
        if q["id"] in seen_questions:
            report["discrepancies"].append({q["id"]: "duplicate question id"})
            continue
        seen_questions.add(q["id"])
        storage.insert_question(conn, q, q.get("_source", "unknown"))
        report["questions"] += 1
        if q["topic"] not in fixtures["owners"]:
            report["missing_owner_topics"].append(q["topic"])

    conn.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Load fixtures into the workspace database")
    parser.add_argument("--reset", action="store_true", help="clear all tables first")
    parser.add_argument("--db", default=None, help="database path override")
    args = parser.parse_args()

    conn = storage.connect(args.db)
    report = load(conn, reset=args.reset)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
