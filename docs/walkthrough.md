# Walkthrough — Questionnaire Evidence & Review Workspace

Written walkthrough, which the brief accepts in place of a recording. Roughly four minutes
to read aloud.

---

## The problem

A sales team answers buyer questionnaires from the company's own documents. Two things go
wrong when a language model does this unsupervised.

It answers questions the documents do not cover. Asked *"Is JSON export available?"* when
nothing anywhere mentions JSON, a model will usually say yes or no. Both are inventions —
the honest answer is that nobody knows, and a human has to find out.

It answers from text that has been replaced. The documents contain `EXPORT-v1`, saying CSV
export is available on every plan, and `EXPORT-v2`, which explicitly replaces it and limits
CSV export to paid plans. Answering from the old one tells a buyer something untrue.

So the interesting part of this task is not generating text. It is deciding what counts as
evidence, and proving each answer against it.

## The shape of the solution

Five steps per question: reuse check, model draft, evidence validation, persist, review.

The model does one job — read the passages, propose an answer, cite the passages it used and
quote from them. Everything else is deterministic code, because everything else can be.

**Authority is resolved before the model sees anything.** A document is authoritative when
no other document declares `supersedes` pointing at it. That is a graph traversal, so
`app/loader.py` does it and stores the result. The model receives passages already tagged
`authority="authoritative"` or `authority="superseded"`.

Dates play no part, deliberately. In the supplied seed, four current documents share the
date `2026-08-01`, and the `RETENTION-v2` document I added is authoritative despite being
older than several of them. Sorting by date gets both wrong. The rules say authority comes
from the `supersedes` field, and that is the only thing used.

## Checking the model's work

Nothing the model says about its own evidence is taken at face value. Five checks in
`app/validation.py`:

1. **Existence** — every cited passage id is real.
2. **Authority** — every citation comes from an authoritative document.
3. **Verbatim** — every quoted excerpt literally occurs in the passage it is attributed to.
   If the model paraphrased inside a quotation, the citation is rejected.
4. **Coverage** — the cited passages lexically cover the question's content words.
5. **Shape** — an "answered" verdict has answer text and at least one surviving citation.

Any failure downgrades the answer to `unresolved` and routes it to the topic owner from the
supplied mapping. Downgrading is one-way: validation can turn an answer into a review item,
never the reverse.

Check four is the one that does the real work here. For *"Is JSON export available?"* the
word `json` appears in no passage at all, so the topic is undocumented. For *"Can free-plan
users invite team members?"* the words `free` and `plan` do exist in the corpus but not in
the cited passage about invites, so the answer would rest on partial evidence. Both are
routed for review rather than answered.

That check is lexical, and I want to be direct about the cost. Ask *"In which currency are
subscriptions billed?"* and it routes for review, because `currency` appears nowhere even
though `BILLING-v1` says USD. That is a false unresolved. I took the trade knowingly: for a
buyer-facing questionnaire, an over-cautious reviewer queue is cheaper than a confident
wrong answer. The fix is embeddings or an entailment check, with the lexical test kept as a
cheap pre-filter. I did not add a synonym list, because inventing product vocabulary is
exactly what the exercise rules forbid.

## Approval, reuse and staleness

Only an approved answer may be reused, and an edit alone is not an approval — a reviewer's
correction stays a `draft` until somebody signs it off.

Approval stores the answer **and a snapshot of the version of every document it cites**.
That snapshot is what makes staleness detectable later: compare it with the live versions,
and any difference means the ground underneath the approval has moved. The answer becomes
`needs_review` and stops being reusable, which is checked on every queue load rather than
only when someone happens to open that row.

## The mistake I made, and how I found it

The staleness check originally compared version numbers only. I wrote a script with two
modes: `--mode bump`, which raises `EXPORT-v2` from version 2 to 3, and `--mode supersede`,
which inserts an `EXPORT-v3` that replaces it. The bump mode worked. I ran the supersede
mode rather than assuming it worked too, and it reported `"flagged_needs_review": []`.

It was right by its own logic and wrong about the world: inserting `EXPORT-v3` leaves
`EXPORT-v2` at version 2, so no version changed — but the approved answer was now resting on
a passage that rule 2 forbids answering from.

The fix was `cited_authority_lost()` alongside the version comparison, so an approval goes
stale for either reason. The supplied rules only mention version changes, so I recorded the
decision as an explicit assumption in the README rather than quietly widening the rule.

That is the part of this exercise I would most want to be judged on. The code was internally
consistent and passed its own checks; what caught it was running the less obvious path and
reading the output.

## What I did not do

No embeddings, no PDF parsing, no CRM integration, no cloud deployment, no agent framework.
The brief lists those as optional and says optional work does not compensate for missing
required functionality, so the required flow and its checks came first.

The committed check report was produced with the offline stub, not a real model. It shows
the pipeline and the review logic behave as I derived by hand; it shows nothing about model
quality, and I have said so everywhere it appears.

Five fictional documents and thirteen questions are a demonstration set. They are evidence
that the rules are implemented, not evidence of accuracy on other data.
