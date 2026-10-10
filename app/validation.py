"""Evidence validation: code checks on every model draft.

The model's citations and quotes are not trusted as given. Each draft goes through five
checks, all of them deterministic:

  C1 existence   every cited passage id is a real passage
  C2 authority   every cited passage belongs to an authoritative document, and any conflict
                 the model reports is with a superseded passage, not another current one (rule 2)
  C3 verbatim    every quoted excerpt literally occurs in the passage it is attributed to
  C4 coverage    the cited passages lexically cover the question's content words (rule 1)
  C5 shape       an "answered" verdict actually has answer text and at least one citation

Any failure makes the answer "unresolved" and routes it to the topic owner from the
supplied mapping (rule 4). The validator can only move an answer to review; it never turns
an unresolved answer into an answered one.

C4 catches an answer built on a passage that does not address the question. It compares
words, not meaning, so it can be wrong in both directions - see the limitations section of
README.md.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import config, storage


# --- lexical helpers ----------------------------------------------------------

def normalize_tokens(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop function words, strip simple plurals.

    Plural stripping only: no real stemmer and no synonyms. A synonym list would amount to
    inventing domain vocabulary, which domain.md forbids.
    """
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text or "")
    tokens = set()
    for raw in cleaned.split():
        if raw in config.STOPWORDS or raw.isdigit():
            continue
        token = raw[:-1] if len(raw) > 3 and raw.endswith("s") else raw
        if token and token not in config.STOPWORDS:
            tokens.add(token)
    return tokens


def _normalize_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def excerpt_is_verbatim(excerpt: str, passage_text: str) -> bool:
    """Whitespace-insensitive, case-insensitive substring test.

    Case and whitespace are forgiven because they carry no meaning here; wording is not.
    """
    needle = _normalize_whitespace(excerpt).casefold()
    haystack = _normalize_whitespace(passage_text).casefold()
    return bool(needle) and needle in haystack


# --- the validator ------------------------------------------------------------

def validate(
    conn: sqlite3.Connection,
    question: dict[str, Any] | sqlite3.Row,
    draft: dict[str, Any],
    *,
    strict_coverage: bool | None = None,
) -> dict[str, Any]:
    """Return the validated outcome. Never raises on model content; it reports instead."""
    strict = config.STRICT_COVERAGE if strict_coverage is None else strict_coverage

    question_text = question["text"]
    topic = question["topic"]

    passages = {p["id"]: p for p in storage.all_passages(conn)}
    corpus_tokens: set[str] = set()
    for p in passages.values():
        corpus_tokens |= normalize_tokens(p["text"])

    cited: list[str] = list(draft.get("cited_passage_ids") or [])
    conflicting: list[str] = list(draft.get("conflicting_passage_ids") or [])
    excerpts: dict[str, str] = dict(draft.get("quoted_excerpts") or {})

    warnings: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    # C1 existence -------------------------------------------------------------
    unknown_cited = [pid for pid in cited if pid not in passages]
    checks["all_citations_exist"] = not unknown_cited
    if unknown_cited:
        warnings.append(
            {
                "code": "citation_not_found",
                "message": "The model cited passage ids that do not exist.",
                "passage_ids": unknown_cited,
            }
        )

    unknown_conflicting = [pid for pid in conflicting if pid not in passages]
    if unknown_conflicting:
        warnings.append(
            {
                "code": "conflict_not_found",
                "message": "The model listed conflicting passage ids that do not exist.",
                "passage_ids": unknown_conflicting,
            }
        )

    known_cited = [pid for pid in cited if pid in passages]
    known_conflicting = [pid for pid in conflicting if pid in passages]

    # C2 authority -------------------------------------------------------------
    superseded_cited = [pid for pid in known_cited if not passages[pid]["is_authoritative"]]
    checks["all_citations_current"] = not superseded_cited
    if superseded_cited:
        warnings.append(
            {
                "code": "citation_superseded",
                "message": (
                    "The model answered from a passage whose document has been explicitly "
                    "superseded. Rule 2 forbids using it as the basis for an answer."
                ),
                "passage_ids": superseded_cited,
            }
        )

    # A conflict with a superseded passage is resolved by metadata. A conflict with another
    # authoritative passage is not: no supersedes link says which one wins, so the question
    # is left for a reviewer instead of picking a side.
    current_conflicts = [pid for pid in known_conflicting if passages[pid]["is_authoritative"]]
    checks["conflicts_resolved_by_metadata"] = not current_conflicts
    if current_conflicts:
        warnings.append(
            {
                "code": "unresolved_conflict",
                "message": (
                    "The model reported a conflict with a passage from a current document. "
                    "No supersedes field decides between them, so a reviewer must."
                ),
                "passage_ids": current_conflicts,
            }
        )

    authoritative_cited = [pid for pid in known_cited if passages[pid]["is_authoritative"]]

    # C3 verbatim --------------------------------------------------------------
    missing_excerpt = [pid for pid in authoritative_cited if pid not in excerpts]
    bad_excerpt = [
        pid
        for pid in authoritative_cited
        if pid in excerpts and not excerpt_is_verbatim(excerpts[pid], passages[pid]["text"])
    ]
    checks["all_excerpts_verified"] = not missing_excerpt and not bad_excerpt
    if missing_excerpt:
        warnings.append(
            {
                "code": "excerpt_missing",
                "message": "A cited passage came with no quoted excerpt to check.",
                "passage_ids": missing_excerpt,
            }
        )
    if bad_excerpt:
        warnings.append(
            {
                "code": "excerpt_not_verbatim",
                "message": (
                    "A quoted excerpt does not occur in the passage it is attributed to. "
                    "The model paraphrased or invented the quotation."
                ),
                "passage_ids": bad_excerpt,
            }
        )

    verified_cited = [
        pid for pid in authoritative_cited if pid not in missing_excerpt and pid not in bad_excerpt
    ]

    # C4 coverage --------------------------------------------------------------
    question_tokens = normalize_tokens(question_text)
    cited_tokens: set[str] = set()
    for pid in verified_cited:
        cited_tokens |= normalize_tokens(passages[pid]["text"])

    undocumented_terms = sorted(question_tokens - corpus_tokens)
    uncovered_terms = sorted((question_tokens & corpus_tokens) - cited_tokens)

    checks["undocumented_terms"] = undocumented_terms
    checks["uncovered_terms"] = uncovered_terms
    checks["coverage_enforced"] = strict

    if undocumented_terms:
        warnings.append(
            {
                "code": "undocumented_term",
                "message": (
                    "These words from the question appear in no supplied passage, so the "
                    f"topic is undocumented: {', '.join(undocumented_terms)}."
                ),
                "passage_ids": [],
            }
        )
    if uncovered_terms:
        warnings.append(
            {
                "code": "uncovered_term",
                "message": (
                    "These words from the question are documented elsewhere but not in the "
                    f"cited evidence: {', '.join(uncovered_terms)}."
                ),
                "passage_ids": [],
            }
        )

    # C5 shape -----------------------------------------------------------------
    model_status = draft.get("status", "unresolved")
    answer_text = (draft.get("answer") or "").strip()
    if model_status == "answered" and not answer_text:
        warnings.append(
            {
                "code": "empty_answer",
                "message": "The model reported an answer but returned no answer text.",
                "passage_ids": [],
            }
        )
    if model_status == "answered" and not verified_cited:
        warnings.append(
            {
                "code": "no_verified_evidence",
                "message": "No cited passage survived the evidence checks.",
                "passage_ids": [],
            }
        )

    # --- verdict --------------------------------------------------------------
    blocking = {
        "citation_not_found",
        "citation_superseded",
        "unresolved_conflict",
        "excerpt_missing",
        "excerpt_not_verbatim",
        "empty_answer",
        "no_verified_evidence",
    }
    if strict:
        blocking |= {"undocumented_term", "uncovered_term"}

    triggered = sorted({w["code"] for w in warnings} & blocking)
    checks["blocking_warnings"] = triggered

    if model_status == "unresolved" or triggered:
        reason = _build_reason(
            model_status=model_status,
            model_reason=draft.get("reason", ""),
            triggered=triggered,
            undocumented_terms=undocumented_terms,
            uncovered_terms=uncovered_terms,
        )
        owner = storage.owner_for_topic(conn, topic)
        if owner is None:
            warnings.append(
                {
                    "code": "owner_not_mapped",
                    "message": (
                        f"Topic {topic!r} has no entry in the supplied owners mapping, so no "
                        "reviewer was assigned. Rule 4 forbids inventing one."
                    ),
                    "passage_ids": [],
                }
            )
        # Citations are cleared because none of them is being relied on. The ids stay
        # visible to the reviewer through the warnings and the stored excerpts.
        return {
            "status": "unresolved",
            "answer_text": "",
            "cited_passage_ids": [],
            "conflicting_passage_ids": [],
            "excerpts": excerpts,
            "warnings": warnings,
            "checks": checks,
            "owner": owner,
            "reason": reason,
            "model_proposed": {
                "status": model_status,
                "answer": answer_text,
                "cited_passage_ids": cited,
                "conflicting_passage_ids": conflicting,
            },
        }

    return {
        "status": "answered",
        "answer_text": answer_text,
        "cited_passage_ids": verified_cited,
        "conflicting_passage_ids": known_conflicting,
        "excerpts": {pid: excerpts[pid] for pid in verified_cited if pid in excerpts},
        "warnings": warnings,
        "checks": checks,
        "owner": None,
        "reason": "",
        "model_proposed": None,
    }


def _build_reason(
    *,
    model_status: str,
    model_reason: str,
    triggered: list[str],
    undocumented_terms: list[str],
    uncovered_terms: list[str],
) -> str:
    parts: list[str] = []
    if "undocumented_term" in triggered:
        parts.append(
            "The question refers to something undocumented in the supplied passages "
            f"({', '.join(undocumented_terms)}); it is unknown rather than unsupported."
        )
    if "uncovered_term" in triggered:
        parts.append(
            "The cited evidence does not cover part of the question "
            f"({', '.join(uncovered_terms)})."
        )
    if "citation_superseded" in triggered:
        parts.append("The proposed answer relied on an explicitly superseded passage.")
    if "unresolved_conflict" in triggered:
        parts.append(
            "Current documents disagree and no supersedes field decides between them."
        )
    if "excerpt_not_verbatim" in triggered or "excerpt_missing" in triggered:
        parts.append("A quoted excerpt could not be verified against its passage.")
    if "citation_not_found" in triggered:
        parts.append("A cited passage id does not exist.")
    if "no_verified_evidence" in triggered or "empty_answer" in triggered:
        parts.append("No checkable evidence supported the proposed answer.")

    if model_status == "unresolved" and model_reason:
        parts.append(f"Model reported: {model_reason}")
    elif model_status == "unresolved" and not parts:
        parts.append("The model reported that the passages do not establish an answer.")

    return " ".join(parts) if parts else "Routed for review."
