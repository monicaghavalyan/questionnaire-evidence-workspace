"""Change a document's version, to demonstrate reference case RC-5.

Rule 3: "Mark an approved answer for review if a referenced document version changes."
This script performs that change so the behaviour can be shown live.

    python scripts/bump_source_version.py --mode bump
        Raise EXPORT-v2 from version 2 to version 3. The text is untouched; only the
        version moves. This is the literal situation rule 3 describes.

    python scripts/bump_source_version.py --mode supersede
        Insert EXPORT-v3 from data/source-change/EXPORT-v3.json, which explicitly
        supersedes EXPORT-v2, and re-resolve authority. EXPORT-v2 stops being
        authoritative, so answers citing it need review as well.

    python scripts/bump_source_version.py --mode reset
        Reload the fixtures, returning documents to their starting versions. Reviewer
        state in the answers table is left alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import loader, review, storage  # noqa: E402
from app.config import SOURCE_CHANGE_DIR  # noqa: E402

TARGET_DOCUMENT = "EXPORT-v2"


def _reresolve_authority(conn) -> None:
    """Recompute is_authoritative / superseded_by for every document now in the database."""
    rows = conn.execute("SELECT id, version, date, status, supersedes FROM documents").fetchall()
    documents = [dict(r) for r in rows]
    for doc in loader.resolve_authority(documents):
        conn.execute(
            "UPDATE documents SET superseded_by = ?, is_authoritative = ? WHERE id = ?",
            (doc["superseded_by"], 1 if doc["is_authoritative"] else 0, doc["id"]),
        )
    conn.commit()


def mode_bump(conn) -> dict:
    row = conn.execute(
        "SELECT version FROM documents WHERE id = ?", (TARGET_DOCUMENT,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"{TARGET_DOCUMENT} is not loaded. Run: python -m app.loader --reset")
    before = int(row["version"])
    after = before + 1
    storage.update_document_version(conn, TARGET_DOCUMENT, after)
    return {"action": "bump", "document": TARGET_DOCUMENT, "from": before, "to": after}


def mode_supersede(conn) -> dict:
    path = SOURCE_CHANGE_DIR / "EXPORT-v3.json"
    with path.open(encoding="utf-8") as fh:
        doc = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}

    doc["superseded_by"] = None
    doc["is_authoritative"] = True
    storage.insert_document(conn, doc, source_file=path.name)
    for passage in doc["passages"]:
        storage.insert_passage(conn, passage["id"], doc["id"], passage["text"])
    conn.execute(
        "UPDATE documents SET status = 'superseded' WHERE id = ?", (doc["supersedes"],)
    )
    conn.commit()
    _reresolve_authority(conn)
    return {
        "action": "supersede",
        "inserted": doc["id"],
        "now_superseded": doc["supersedes"],
    }


def mode_reset(conn) -> dict:
    conn.execute("DELETE FROM passages")
    conn.execute("DELETE FROM documents")
    conn.commit()
    report = loader.load(conn, reset=False)
    return {"action": "reset", "documents": report["documents"], "passages": report["passages"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("bump", "supersede", "reset"), default="bump")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    conn = storage.connect(args.db)
    result = {"bump": mode_bump, "supersede": mode_supersede, "reset": mode_reset}[args.mode](conn)

    # Immediately re-run the approval check so the effect is visible in the output.
    result["flagged_needs_review"] = review.refresh_source_change_flags(conn)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
