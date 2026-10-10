"""SQLite persistence.

Three groups of tables:
  documents / passages  - the evidence corpus, with authority resolved at load time
  questions             - the questionnaire
  answers               - current state of each question
  answer_events         - append-only audit trail, so "what changed" is always answerable

Everything a reviewer does is written here, which is what makes the state survive a reload.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id                TEXT PRIMARY KEY,
    version           INTEGER NOT NULL,
    date              TEXT NOT NULL,
    status            TEXT NOT NULL,          -- current | superseded
    supersedes        TEXT,                   -- document id this one explicitly replaces
    superseded_by     TEXT,                   -- filled in by the loader from supersedes
    is_authoritative  INTEGER NOT NULL,       -- resolved in code, never by the model
    source_file       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passages (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(id),
    text         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id           TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    text         TEXT NOT NULL,
    source_file  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owners (
    topic  TEXT PRIMARY KEY,
    owner  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    question_id              TEXT PRIMARY KEY REFERENCES questions(id),
    question_text            TEXT NOT NULL,
    answer_text              TEXT,
    status                   TEXT NOT NULL,   -- draft|approved|needs_review|unresolved|error
    origin                   TEXT NOT NULL,   -- model_draft|human_edit|reused_approved|system
    cited_passage_ids        TEXT NOT NULL DEFAULT '[]',
    conflicting_passage_ids  TEXT NOT NULL DEFAULT '[]',
    excerpts                 TEXT NOT NULL DEFAULT '{}',
    warnings                 TEXT NOT NULL DEFAULT '[]',
    checks                   TEXT NOT NULL DEFAULT '{}',
    owner                    TEXT,
    reason                   TEXT,
    source_versions          TEXT NOT NULL DEFAULT '{}',  -- snapshot taken at approval
    model_mode               TEXT,
    model_id                 TEXT,
    cache_key                TEXT,
    reviewer_note            TEXT,
    reused_from              TEXT,            -- question_id whose approved answer was reused
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    approved_at              TEXT,
    approved_by              TEXT
);

CREATE TABLE IF NOT EXISTS answer_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id  TEXT NOT NULL,
    event        TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_question ON answer_events(question_id);
CREATE INDEX IF NOT EXISTS idx_passages_document ON passages(document_id);
"""

JSON_COLUMNS = (
    "cited_passage_ids",
    "conflicting_passage_ids",
    "excerpts",
    "warnings",
    "checks",
    "source_versions",
)

_LIST_COLUMNS = frozenset({"cited_passage_ids", "conflicting_passage_ids", "warnings"})


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def reset(conn: sqlite3.Connection) -> None:
    for table in ("answer_events", "answers", "owners", "questions", "passages", "documents"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


# --- reads --------------------------------------------------------------------

def all_passages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every passage with its document's authority metadata, authoritative ones first."""
    rows = conn.execute(
        """
        SELECT p.id, p.text, p.document_id,
               d.version, d.date, d.status, d.supersedes, d.superseded_by, d.is_authoritative
        FROM passages p
        JOIN documents d ON d.id = p.document_id
        ORDER BY d.is_authoritative DESC, p.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def document_versions(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT id, version FROM documents").fetchall()
    return {r["id"]: r["version"] for r in rows}


def passage_owner_document(conn: sqlite3.Connection, passage_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT d.* FROM passages p JOIN documents d ON d.id = p.document_id
        WHERE p.id = ?
        """,
        (passage_id,),
    ).fetchone()


def get_question(conn: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def all_questions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM questions ORDER BY id").fetchall()


def owner_for_topic(conn: sqlite3.Connection, topic: str) -> str | None:
    row = conn.execute("SELECT owner FROM owners WHERE topic = ?", (topic,)).fetchone()
    return row["owner"] if row else None


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for col in JSON_COLUMNS:
        if col in out and isinstance(out[col], str):
            try:
                out[col] = json.loads(out[col])
            except json.JSONDecodeError:
                out[col] = [] if col in _LIST_COLUMNS else {}
    return out


def get_answer(conn: sqlite3.Connection, question_id: str) -> dict[str, Any] | None:
    return _decode(
        conn.execute("SELECT * FROM answers WHERE question_id = ?", (question_id,)).fetchone()
    )


def all_answers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM answers ORDER BY question_id").fetchall()
    return [_decode(r) for r in rows]


def find_approved_by_text(
    conn: sqlite3.Connection, question_text: str, exclude_question_id: str | None = None
) -> dict[str, Any] | None:
    """Exact-text lookup of an APPROVED answer. The brief allows exact matching.

    needs_review rows are deliberately excluded: rule 3 forbids reusing an answer whose
    source version has changed until a reviewer looks at it again.
    """
    sql = "SELECT * FROM answers WHERE question_text = ? AND status = 'approved'"
    params: list[Any] = [question_text]
    if exclude_question_id:
        sql += " AND question_id != ?"
        params.append(exclude_question_id)
    sql += " ORDER BY approved_at DESC LIMIT 1"
    return _decode(conn.execute(sql, params).fetchone())


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM answers GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def events(conn: sqlite3.Connection, question_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM answer_events WHERE question_id = ? ORDER BY id", (question_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"])
        except json.JSONDecodeError:
            d["detail"] = {}
        out.append(d)
    return out


# --- writes -------------------------------------------------------------------

def insert_document(conn: sqlite3.Connection, doc: dict[str, Any], source_file: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO documents
            (id, version, date, status, supersedes, superseded_by, is_authoritative, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc["id"],
            int(doc["version"]),
            doc["date"],
            doc["status"],
            doc.get("supersedes"),
            doc.get("superseded_by"),
            1 if doc["is_authoritative"] else 0,
            source_file,
        ),
    )


def insert_passage(conn: sqlite3.Connection, passage_id: str, document_id: str, text: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO passages (id, document_id, text) VALUES (?, ?, ?)",
        (passage_id, document_id, text),
    )


def insert_question(conn: sqlite3.Connection, q: dict[str, Any], source_file: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO questions (id, topic, text, source_file) VALUES (?, ?, ?, ?)",
        (q["id"], q["topic"], q["text"], source_file),
    )


def set_owner(conn: sqlite3.Connection, topic: str, owner: str) -> None:
    conn.execute("INSERT OR REPLACE INTO owners (topic, owner) VALUES (?, ?)", (topic, owner))


def upsert_answer(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    payload = dict(record)
    for col in JSON_COLUMNS:
        if col in payload and not isinstance(payload[col], str):
            payload[col] = json.dumps(payload[col], ensure_ascii=False)
    existing = conn.execute(
        "SELECT question_id FROM answers WHERE question_id = ?", (payload["question_id"],)
    ).fetchone()
    if existing:
        cols = [k for k in payload if k != "question_id"]
        conn.execute(
            f"UPDATE answers SET {', '.join(f'{c} = ?' for c in cols)} WHERE question_id = ?",
            [payload[c] for c in cols] + [payload["question_id"]],
        )
    else:
        cols = list(payload)
        conn.execute(
            f"INSERT INTO answers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [payload[c] for c in cols],
        )
    conn.commit()


def log_event(
    conn: sqlite3.Connection, question_id: str, event: str, detail: dict[str, Any], when: str
) -> None:
    conn.execute(
        "INSERT INTO answer_events (question_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
        (question_id, event, json.dumps(detail, ensure_ascii=False), when),
    )
    conn.commit()


def update_document_version(
    conn: sqlite3.Connection, document_id: str, new_version: int
) -> None:
    conn.execute("UPDATE documents SET version = ? WHERE id = ?", (new_version, document_id))
    conn.commit()
