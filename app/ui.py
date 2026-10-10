"""Streamlit review workspace.

    streamlit run app/ui.py

What a reviewer needs to see on one screen: the question, the proposed answer, the exact
evidence behind it, and anything the validator objected to. Everything a reviewer does is
written straight to SQLite, so closing the browser loses nothing.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import streamlit as st

# `streamlit run app/ui.py` puts app/ on sys.path, not the repository root, so the
# `from app import ...` imports below would fail without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, loader, model_client, pipeline, review, storage  # noqa: E402

STATUS_LABELS = {
    "draft": "Draft - awaiting review",
    "approved": "Approved",
    "needs_review": "Needs review - source changed",
    "unresolved": "Unresolved - routed to owner",
    "error": "Error",
}

STATUS_COLOURS = {
    "draft": "orange",
    "approved": "green",
    "needs_review": "red",
    "unresolved": "violet",
    "error": "red",
}


# --- connection ---------------------------------------------------------------

@st.cache_resource
def get_conn() -> sqlite3.Connection:
    # check_same_thread=False because Streamlit reruns the script on its own threads.
    conn = storage.connect()
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_loaded(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
    if count == 0:
        loader.load(conn, reset=True)


# --- small renderers ----------------------------------------------------------

def status_badge(status: str) -> str:
    colour = STATUS_COLOURS.get(status, "grey")
    return f":{colour}[**{STATUS_LABELS.get(status, status)}**]"


def render_mode_banner() -> None:
    mode = config.MODEL_MODE
    recorded = len(model_client.cache_inventory())
    if mode == "stub":
        st.warning(
            "**MODEL_MODE=stub** - answers come from an offline keyword stand-in, not a "
            "language model. Use `MODEL_MODE=live` once to record real responses, then "
            "`MODEL_MODE=replay` to serve them.",
            icon=":material/warning:",
        )
    elif mode == "replay":
        st.info(
            f"**MODEL_MODE=replay** - serving {recorded} recorded response(s) from "
            f"`{config.CACHE_DIR.name}/`. No network calls, no API key needed.",
            icon=":material/history:",
        )
    else:
        st.success(
            f"**MODEL_MODE=live** - calling `{config.MODEL_ID}`. Every response is recorded "
            f"to `{config.CACHE_DIR.name}/` for replay.",
            icon=":material/cloud:",
        )


def render_warnings(warnings: list[dict[str, Any]]) -> None:
    if not warnings:
        st.caption("No validation warnings.")
        return
    for w in warnings:
        icon = ":material/error:" if w["code"] == "source_version_changed" else ":material/report:"
        st.warning(f"**{w['code']}** - {w['message']}", icon=icon)


def render_evidence(conn: sqlite3.Connection, answer: dict[str, Any]) -> None:
    cited = answer.get("cited_passage_ids") or []
    excerpts = answer.get("excerpts") or {}
    passages = {p["id"]: p for p in storage.all_passages(conn)}

    if cited:
        st.markdown("**Evidence used**")
        for pid in cited:
            p = passages.get(pid)
            if p is None:
                st.error(f"`{pid}` - cited passage no longer exists")
                continue
            st.markdown(
                f"`{pid}` &nbsp; document **{p['document_id']}** v{p['version']} "
                f"({p['date']}) &nbsp; :green[authoritative]",
                unsafe_allow_html=True,
            )
            st.markdown(f"> {p['text']}")
            quoted = excerpts.get(pid)
            if quoted:
                verified = quoted.strip().casefold() in p["text"].casefold()
                mark = ":green[verified verbatim]" if verified else ":red[NOT verbatim]"
                st.caption(f"Model quoted: “{quoted}” - {mark}")
    else:
        st.markdown("**Evidence used**")
        st.caption("None. No passage survived the evidence checks.")

    conflicts = answer.get("conflicting_passage_ids") or []
    if conflicts:
        st.markdown("**Conflicting earlier text (kept visible, not used)**")
        for pid in conflicts:
            p = passages.get(pid)
            if p is None:
                continue
            doc = storage.passage_owner_document(conn, pid)
            replaced_by = doc["superseded_by"] if doc else "?"
            st.markdown(
                f"`{pid}` &nbsp; document **{p['document_id']}** v{p['version']} "
                f"({p['date']}) &nbsp; :red[superseded by {replaced_by}]",
                unsafe_allow_html=True,
            )
            st.markdown(f"> {p['text']}")


def render_history(conn: sqlite3.Connection, question_id: str) -> None:
    history = storage.events(conn, question_id)
    if not history:
        st.caption("No history yet.")
        return
    for event in history:
        with st.container(border=True):
            st.markdown(f"**{event['event']}** &nbsp; `{event['created_at']}`")
            if event["detail"]:
                st.json(event["detail"], expanded=False)


# --- main ---------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Questionnaire evidence workspace", layout="wide")
    conn = get_conn()
    ensure_loaded(conn)

    st.title("Questionnaire evidence and review workspace")
    render_mode_banner()

    summary = pipeline.queue_summary(conn)
    counts = summary["counts"]

    # --- sidebar -------------------------------------------------------------
    with st.sidebar:
        st.header("Queue")
        c1, c2 = st.columns(2)
        c1.metric("Questions", summary["total_questions"])
        c2.metric("Approved", counts.get("approved", 0))
        c1.metric("Drafts", counts.get("draft", 0))
        c2.metric("Unresolved", counts.get("unresolved", 0))
        c1.metric("Needs review", counts.get("needs_review", 0))
        c2.metric("Errors", counts.get("error", 0))
        st.caption(
            f"Answered excluding reuse: **{summary['answered_excluding_reuse']}**. "
            f"Reused approved answers: **{len(summary['reused_question_ids'])}** "
            f"({', '.join(summary['reused_question_ids']) or 'none'}). "
            "A reuse is not counted as new answered work."
        )

        st.divider()
        st.subheader("Actions")
        if st.button("Draft all unanswered", width="stretch"):
            with st.spinner("Drafting..."):
                pipeline.draft_all(conn)
            st.rerun()
        if st.button("Re-check approved sources", width="stretch"):
            flagged = review.refresh_source_change_flags(conn)
            st.success(f"{len(flagged)} approval(s) flagged." if flagged else "All approvals current.")
        if st.button("Reload fixtures (keeps reviews)", width="stretch"):
            conn.execute("DELETE FROM passages")
            conn.execute("DELETE FROM documents")
            conn.commit()
            loader.load(conn, reset=False)
            st.rerun()

        st.divider()
        st.subheader("Filter")
        chosen = st.multiselect(
            "Status",
            options=list(STATUS_LABELS),
            default=list(STATUS_LABELS),
            format_func=lambda s: STATUS_LABELS[s],
        )

        if summary["unresolved_by_owner"]:
            st.divider()
            st.subheader("Routed to")
            for owner, n in sorted(summary["unresolved_by_owner"].items()):
                st.write(f"- {owner}: **{n}**")

        if summary["warning_counts"]:
            st.divider()
            st.subheader("Validator findings")
            for code, n in sorted(summary["warning_counts"].items(), key=lambda kv: -kv[1]):
                st.write(f"- `{code}`: **{n}**")

    # --- queue ---------------------------------------------------------------
    questions = storage.all_questions(conn)
    rows = []
    for q in questions:
        answer = storage.get_answer(conn, q["id"])
        status = answer["status"] if answer else "not drafted"
        if answer and status not in chosen:
            continue
        if not answer and "draft" not in chosen:
            continue
        rows.append((q, answer, status))

    st.subheader(f"Review queue ({len(rows)} shown)")
    if not rows:
        st.info("Nothing matches this filter.")
        return

    labels = {}
    for q, answer, status in rows:
        tag = STATUS_LABELS.get(status, status)
        reused = " (reused)" if answer and answer["origin"] == "reused_approved" else ""
        labels[q["id"]] = f"{q['id']} - {q['text']}  [{tag}{reused}]"

    selected_id = st.radio(
        "Question", options=[q["id"] for q, _, _ in rows],
        format_func=lambda qid: labels[qid], label_visibility="collapsed",
    )

    question = storage.get_question(conn, selected_id)
    answer = storage.get_answer(conn, selected_id)

    st.divider()
    if answer is None:
        st.markdown(f"### {question['id']} - {question['text']}")
        st.caption(f"Topic: `{question['topic']}` - not drafted yet")
        if st.button("Draft this question", type="primary"):
            pipeline.draft_question(conn, selected_id)
            st.rerun()
        return

    # --- side by side: question | answer | evidence ---------------------------
    left, middle, right = st.columns([1, 1.2, 1.4], gap="medium")

    with left:
        st.markdown("#### Question")
        st.markdown(f"**{question['id']}**")
        st.info(question["text"])
        st.caption(f"Topic: `{question['topic']}`")
        st.caption(f"Source file: `{question['source_file']}`")
        st.markdown(status_badge(answer["status"]))
        if answer["owner"]:
            st.caption(f"Routed to: **{answer['owner']}**")
        if answer["reused_from"]:
            st.success(
                f"Reused the answer approved for **{answer['reused_from']}** "
                f"on {answer['approved_at']}.",
                icon=":material/recycling:",
            )
        st.caption(
            f"Origin: `{answer['origin']}` - model: "
            f"`{answer['model_id'] or 'n/a'}` ({answer['model_mode'] or 'n/a'})"
        )
        if answer["cache_key"]:
            st.caption(f"Recorded response: `{answer['cache_key'][:16]}...`")

    with middle:
        st.markdown("#### Proposed answer")
        if answer["status"] == "unresolved":
            st.error(answer["reason"] or "Unresolved.", icon=":material/help:")
        elif answer["answer_text"]:
            st.success(answer["answer_text"])
        else:
            st.caption("No answer text.")

        if answer["approved_at"]:
            st.caption(f"Approved by **{answer['approved_by']}** at `{answer['approved_at']}`")
            if answer["source_versions"]:
                st.caption(f"Approved against source versions: `{answer['source_versions']}`")
        if answer["reviewer_note"]:
            st.caption(f"Reviewer note: {answer['reviewer_note']}")

        st.markdown("##### Reviewer actions")
        edited = st.text_area(
            "Correct the wording",
            value=answer["answer_text"] or "",
            height=110,
            key=f"edit_{selected_id}",
        )
        note = st.text_input("Note (optional)", key=f"note_{selected_id}")

        b1, b2, b3 = st.columns(3)
        if b1.button("Save edit", key=f"save_{selected_id}", width="stretch"):
            if not edited.strip():
                st.error("Nothing to save.")
            else:
                review.edit(conn, selected_id, edited.strip(), note or None)
                st.rerun()
        if b2.button("Approve", key=f"approve_{selected_id}", type="primary", width="stretch"):
            try:
                if edited.strip() and edited.strip() != (answer["answer_text"] or "").strip():
                    review.edit(conn, selected_id, edited.strip(), note or None)
                review.approve(conn, selected_id)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if b3.button("Leave unresolved", key=f"park_{selected_id}", width="stretch"):
            review.park_unresolved(conn, selected_id, note or "Parked by reviewer.")
            st.rerun()

        if st.button("Re-draft from the model", key=f"redraft_{selected_id}"):
            try:
                pipeline.draft_question(conn, selected_id, force=True)
                st.rerun()
            except Exception as exc:
                st.error(f"{type(exc).__name__}: {exc}")

    with right:
        st.markdown("#### Evidence and checks")
        render_warnings(answer.get("warnings") or [])
        render_evidence(conn, answer)

        checks = answer.get("checks") or {}
        if checks:
            with st.expander("Validator detail"):
                st.json(checks)

        with st.expander("History"):
            render_history(conn, selected_id)


# Streamlit executes this file top to bottom on every rerun, so main() is called directly.
main()
