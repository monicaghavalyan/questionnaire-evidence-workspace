# Walkthrough — Questionnaire Evidence & Review Workspace

## The problem

A sales team answers buyer questionnaires using the company's own documents. A language
model working alone makes two kinds of mistake.

It answers questions the documents do not cover. Asked *"Is JSON export available?"* when
no document mentions JSON, a model will usually say yes or no. Either answer is made up.
The correct answer is "unknown", and a person has to find out.

It answers from text that has been replaced. The documents contain `EXPORT-v1`, which says
CSV export is available on every plan, and `EXPORT-v2`, which replaces it and limits CSV
export to paid plans. An answer based on the old document tells a buyer something untrue.

So the main work is not generating text. It is deciding what counts as evidence and checking
each answer against it.

## How the solution works

Each question goes through five steps: reuse check, model draft, evidence validation,
saving, and review.

The model has one job: read the passages, propose an answer, and cite and quote the
passages it used. Everything else is ordinary code.

**Which document is current is decided before the model sees anything.** A document is
current when no other document lists it in its `supersedes` field. `app/loader.py` works
this out when the data is loaded, and the model receives each passage already labelled
`authority="authoritative"` or `authority="superseded"`.

Dates are not used for this. In the supplied data, four current documents share the date
`2026-08-01`, and the `RETENTION-v2` document I added is current even though it is older
than several others. Sorting by date would get both cases wrong. The rules say only the
`supersedes` field decides, so that is all the code uses.

## Checking the model's work

The model's answer and citations are checked in code, in `app/validation.py`:

1. **Existence:** every cited passage id is real.
2. **Authority:** every citation comes from a current document, and any conflict the model
   reports is with a superseded passage. If two *current* documents disagree, no
   `supersedes` field settles it, so the question goes to a reviewer.
3. **Verbatim:** every quoted excerpt appears word for word in the passage it is attributed
   to. If the model paraphrased inside a quotation, the citation is rejected.
4. **Coverage:** the cited passages contain the question's content words.
5. **Shape:** an "answered" result has answer text and at least one citation that passed.

If any check fails, the answer becomes `unresolved` and is routed to the topic owner from
the supplied mapping. Validation can only move an answer to review; it never turns an
unresolved answer into an answered one.

The coverage check catches the main risk. For *"Is JSON export available?"* the word `json`
appears in no passage, so the topic is undocumented. For *"Can free-plan users invite team
members?"* the words `free` and `plan` appear in the documents, but not in the passage about
invites, so the answer would only be partly supported. Both go to review.

The coverage check compares words, not meaning, and that has a cost. *"In which currency are
subscriptions billed?"* would go to review, because the word `currency` appears nowhere,
even though `BILLING-v1` says USD. I accepted this: for answers that go to buyers, an extra
item in the review queue costs less than a wrong answer. A better version would compare
meaning, for example with embeddings, and keep the word check as a quick first filter. I
did not add a synonym list, because the exercise rules forbid inventing product vocabulary.

## Approval, reuse and source changes

Only an approved answer can be reused. An edit on its own is not an approval: a reviewer's
correction stays a `draft` until someone approves it.

When an answer is approved, the app saves the answer **and the version of every document it
cites**. Later, it compares those versions with the current ones. If any has changed, the
answer becomes `needs_review` and is no longer reused. This check runs every time the queue
loads, not only when someone opens that question.

## What the real model run caught

The code was generated with AI assistance. Until the end, every check ran against an
offline keyword stub, so it could be developed without an API key, and all five reference
cases passed that way. The first run against the real API crashed immediately:
`Messages.create() got an unexpected keyword argument 'temperature'`. The generated code was
written for an older version of the Anthropic SDK, and the stub never reached that line.

Two things were fixed. `temperature` is now sent in the request body, which the API still
accepts for this model. And API errors now become visible `error` rows instead of
exceptions, because the crash showed that one failure would have stopped the whole batch.

The next attempt tested that second fix with a real failure: the account had no API credit,
and all thirteen questions came back as `error` with the provider's message while the batch
still finished. After credit was added, the run recorded twelve real responses, and the five
reference cases passed again when replayed from them.

The lesson: checks against the offline stub did not test the real integration. A live call
should come early in development, not at the end.

A second problem was found earlier, while testing source changes. The source-change check
compared version numbers only. Adding an `EXPORT-v3` that supersedes `EXPORT-v2`, while
`EXPORT-v2` stays at version 2, was not detected, even though the approved answer now relied
on a passage rule 2 forbids. `cited_authority_lost()` fixed it. The rules only mention
version changes, so this decision is recorded as an assumption in the README.

## What I did not do

No embeddings, PDF parsing, CRM integration, cloud deployment or agent framework. The brief
lists these as optional and says optional work does not make up for missing required
functionality, so the required flow and its checks came first.

The committed check reports replay twelve real responses from `claude-sonnet-4-5`. The
failure-handling report uses hand-written failing responses and says so in its header.

Seven short fictional documents and thirteen questions are a small demonstration set. They
show that the rules are implemented, not how accurate the system would be on other data.
