# Questionnaire Evidence & Review Workspace

Junior AI Engineer take-home, **Alternative C - Answer & approve**.
Starter pack `2026-10-02`, task folder `tasks/evidence/`.

A local workspace that drafts answers to buyer questionnaire questions from a company's own
documents, proves each answer against the exact passage behind it, routes anything it cannot
prove to a named reviewer, and reuses a reviewer's approved wording on the next identical
question until the source underneath it changes.

![Review queue](docs/screenshot-review-queue.png)

---

## 1. Quick start

Python 3.11+ (developed on 3.13.16).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m app.loader --reset                      # load fixtures into SQLite
MODEL_MODE=replay streamlit run app/ui.py         # open the review queue
```

Run the five reference cases:

```bash
MODEL_MODE=replay python checks/run_checks.py
```

**No API key is needed for either command.** `MODEL_MODE=replay` serves the real model
responses recorded in `cache/`. See section 3.

---

## 2. What it does

```
 questions + documents
          |
   [ 1 ] reuse check ........... is there an approved answer to this exact question,
          |                      still backed by the versions it was approved against?
   [ 2 ] model draft ........... one call, whole corpus in context, JSON out
          |
   [ 3 ] evidence validation ... five deterministic checks (section 5)
          |
   [ 4 ] persist ............... SQLite: answer state + append-only audit trail
          |
   [ 5 ] review queue .......... question | answer | evidence, side by side
```

Answer states:

| state | meaning |
|---|---|
| `draft` | a model proposal that passed validation, or a human edit nobody has approved |
| `approved` | a reviewer signed off; **only this state may be reused** |
| `needs_review` | was approved, but a document behind it moved |
| `unresolved` | no checkable evidence; parked with the topic owner |
| `error` | the model could not be reached; the rest of the queue still ran |

---

## 3. Model access and replay

`MODEL_MODE` controls where responses come from.

| mode | behaviour | needs a key |
|---|---|---|
| `live` | calls the provider and **records every response** to `cache/<sha256>.json` | yes |
| `replay` | serves recorded responses, never touches the network — **the default** | no |
| `stub` | offline keyword stand-in for development | no |

The cache key is a SHA-256 of the model id, temperature, max tokens, system prompt and user
prompt. Change any of them and you get a cache miss rather than a stale response quietly
reused. Temperature is 0 so a recorded response fairly represents a repeat call.

Each cache file holds the full request and response, so a reviewer can read exactly what the
model was asked and what it said.

### The stub is not a model run

`MODEL_MODE=stub` scores authoritative passages by keyword overlap and returns the best one.
It exists so the pipeline can be developed and tested without credentials. It is labelled
`stub` in the database, in the UI banner and in the check report, and it is **not** evidence
that the system works with a real model.

> ⚠️ **Before submitting:** run once with `MODEL_MODE=live` and `MODEL_API_KEY` set to
> populate `cache/`, then commit the cache and re-run the checks in `replay`. Replace
> `checks/report-stub.*` with `checks/report-replay.*`. Section 9 tracks this.

```bash
export MODEL_API_KEY=...            # never committed; see ai-workflow/.env.example
MODEL_MODE=live python -c "from app import storage,pipeline; pipeline.draft_all(storage.connect(), force=True)"
MODEL_MODE=replay python checks/run_checks.py --json checks/report-replay.json
```

---

## 4. Layout

```
app/
  config.py        every tunable in one place
  storage.py       SQLite schema, reads and writes
  loader.py        fixtures -> SQLite; resolves document authority in code
  model_client.py  live / replay / stub, with the response cache
  drafting.py      prompt construction and strict JSON parsing
  validation.py    the five evidence checks
  review.py        edit, approve, version snapshots, reuse, staleness
  pipeline.py      the flow above, per question
  ui.py            Streamlit review queue
data/
  starter-pack/    supplied files, UNCHANGED
  additions.json   my additions, with a _why on every entry
  reference-cases.json   five hand-derived cases
  source-change/   EXPORT-v3, applied on demand for RC-5
checks/run_checks.py     runs the five cases, prints expected vs observed
scripts/bump_source_version.py   changes a document version, for RC-5
cache/           recorded real model responses, committed for replay
ai-workflow/     AI configuration manifest and notes
notes/llm-usage.md  tools, models, and one correction
docs/walkthrough.md written walkthrough of the approach
```

---

## 5. How each rule is implemented

Rules are from `data/starter-pack/domain.md`. They are the only source of truth used; no
outside product, security or compliance knowledge was applied.

### Rule 1 — only the supplied documents; undocumented means unknown

The system prompt restricts the model to the supplied passages and states that absence of
documentation means **unknown**, not absent. Because an instruction is not a guarantee, code
checks the result (`app/validation.py`):

| check | what it rejects |
|---|---|
| **C1 existence** | a cited passage id that does not exist |
| **C2 authority** | answering from a passage whose document was superseded |
| **C3 verbatim** | a quoted excerpt that does not occur in the passage it is attributed to |
| **C4 coverage** | an answer whose cited evidence does not cover the question's content words |
| **C5 shape** | an "answered" verdict with no answer text or no surviving citation |

Any failure downgrades the answer to `unresolved`. Downgrading is one-way: validation can
turn an answer into a review item, never the reverse.

C4 is what catches the failure this task is really about. For *"Is JSON export available?"*
the word `json` appears in no passage anywhere, so the question is undocumented. For
*"Can free-plan users invite team members?"* the words `free` and `plan` exist in the corpus
but not in the cited `ACCESS-v1:p1`, so the answer would be built on partial evidence. Both
are routed for review instead of answered.

### Rule 2 — authority comes only from `supersedes`

Resolved in `app/loader.py` before the model sees anything. A document is authoritative when
no other document declares `supersedes` pointing at it. Dates are loaded for display and take
no part in the decision — deliberately, because the seed gives `EXPORT-v2`, `SUPPORT-v1`,
`ACCESS-v1` and `BILLING-v1` the same date, and my added `RETENTION-v2` is authoritative
despite being *older* than several current documents. Sorting by date would get both wrong.

The model is handed each passage pre-labelled `authority="authoritative"` or
`authority="superseded"`. It is not asked to work out authority: that rule is deterministic
and does not belong in a probabilistic component.

Replaced text stays available. Superseded passages are loaded, shown in the UI under
*"Conflicting earlier text (kept visible, not used)"*, and recorded per answer in
`conflicting_passage_ids`.

### Rule 3 — only approved answers are reused

`approve()` stores the answer **and a snapshot of the version of every document it cites**.
That snapshot is what makes staleness detectable later.

- An edit sets the state to `draft` and clears any previous approval, because the approved
  wording no longer matches what is stored.
- `refresh_source_change_flags()` runs on every queue load and before every reuse, so a stale
  approval cannot be served just because nobody opened that row.
- Reuse matches question text exactly, which the brief permits.

### Rule 4 — owners come from the supplied mapping

Every unresolved answer is routed by topic using the supplied `owners` map. A topic with no
entry produces an `owner_not_mapped` warning rather than an invented reviewer.

---

## 6. Data

The supplied seed in `data/starter-pack/` is **unchanged**. Additions are merged on top at
load time from `data/additions.json`.

**Generation method:** hand-written. No random generation was used, so no seed value applies.
Every added document and question carries a `_why` field explaining which check it serves.

| added | why |
|---|---|
| `RETENTION-v1` / `RETENTION-v2` | a second explicitly superseded pair, so authority logic is not tested only by the supplied CSV case. `RETENTION-v2` is deliberately dated *earlier* than several current documents. |
| `Q9` retention | supported answer requiring the new supersession to be resolved |
| `Q10` guaranteed response time | undocumented topic — nothing in the corpus mentions it |
| `Q11` free-plan invites | *partially* documented: invites are covered, plan tiers are not |
| `Q12` billed in USD | a plain supported control case |
| `Q13` | byte-identical text to `Q1`, which drives the reuse check |
| `data/source-change/EXPORT-v3.json` | applied on demand by `scripts/bump_source_version.py` for RC-5 |
| `owners.retention` | the new topic needs a reviewer; taken from the existing reviewer names, not invented as a new role |

Preparation took about 45 minutes, inside the 30–60 minute allowance.

---

## 7. Reference cases

Five cases in `data/reference-cases.json`, one per minimum-demonstration bullet. **Expected
values were derived by hand** from the passages, with the derivation recorded in each case.
The application's own output was never promoted into the answer key.

```bash
MODEL_MODE=replay python checks/run_checks.py --json checks/report-replay.json
```

Each case runs against a fresh throwaway database, so results do not depend on run order.

| case | covers | asserts |
|---|---|---|
| RC-1 | supported question | cites `SUPPORT-v1:p1`, excerpt verified verbatim |
| RC-2 | undocumented capability | `unresolved`, routed to Product reviewer, invents nothing |
| RC-3 | superseded conflict | cites `EXPORT-v2:p1`, shows `EXPORT-v1:p1` as conflict |
| RC-4 | approval and reuse | approved wording reused on `Q13`; an unapproved edit is not |
| RC-5 | source change | approved answer → `needs_review`, survives reload, control stays approved |

Latest result: **5/5 passed** — see `checks/report-stub.txt`
(to be replaced by `report-replay.txt`; see section 9).

---

## 8. Assumptions and ambiguities

Recorded rather than silently resolved, as `domain.md` asks.

1. **`status` vs `supersedes` could disagree.** Each document carries both a `status` field
   and a `supersedes` graph. The rules do not say which wins if they conflict. **Decision:**
   the graph decides; a mismatch is reported as a discrepancy by the loader rather than
   quietly corrected. The supplied seed has no mismatch.

2. **Losing authority without a version change.** Rule 3 covers a referenced document's
   *version* changing. It does not cover a cited document being superseded by a new one while
   its own version stays the same — which is exactly what `--mode supersede` does.
   **Decision:** treated as equivalent, because rule 2 forbids answering from a superseded
   passage, so an approval resting on one must go back for review. *This gap was found while
   testing, not while designing — see `notes/llm-usage.md`.*

3. **What makes two questions "the same".** Not defined. The brief says exact matching is
   sufficient. **Decision:** exact text match, case- and whitespace-sensitive. `Q13` is
   byte-identical to `Q1` and reuses its answer. A paraphrase would not.

4. **How to decide something is undocumented.** Rule 1 says an undocumented feature is
   unknown but does not say how to tell. **Decision:** the lexical coverage test in C4. Its
   limitations are in section 10.

5. **Approving an unresolved answer.** Not addressed by the rules. **Decision:** forbidden —
   an approval should never be recorded against evidence that failed its checks. A reviewer
   must supply wording first, which makes it a `draft`, and can then approve it.

6. **An added topic needs an owner.** `retention` is not in the supplied mapping, so I added
   it to `additions.json` reusing an existing reviewer name. Code never invents one: an
   unmapped topic produces an `owner_not_mapped` warning.

---

## 9. Known limitations

1. **C4 coverage is lexical, not semantic.** It compares normalised word sets with plural
   stripping and a generic English stoplist. It produces false `unresolved` on paraphrases:
   *"In which currency are subscriptions billed?"* is answerable from `BILLING-v1:p1`, but
   `currency` appears in no passage, so it would be routed for review. This was a deliberate
   trade: for a buyer-facing questionnaire, an over-cautious reviewer queue is cheaper than a
   confident wrong answer. A production version would use embeddings or an entailment check
   and keep the lexical test as a cheap pre-filter. No synonym list was added, because
   inventing product vocabulary is what `domain.md` forbids.

2. **No semantic check that the answer text follows from the excerpt.** C3 proves the
   quotation is real; nothing proves the prose conclusion is entailed by it. An LLM-as-judge
   second pass would be the next step, but it would need its own evaluation before being
   trusted.

3. **Reuse is exact-match only.** Any rewording misses, and the model is called again.

4. **Single-passage corpus.** Each supplied document has one passage, so multi-passage
   synthesis within one document is untested.

5. **No concurrency control.** Two reviewers approving the same answer at once would race.
   Out of scope for a local single-reviewer demo.

6. **Check report is from the stub.** `checks/report-stub.*` is the committed result.
   A `replay` run against recorded real responses must replace it before submission.

7. **Optional enhancements not attempted:** no embeddings, no PDF parsing, no CRM
   integration, no sending answers to customers. The brief lists these as optional, and the
   required flow came first.

---

## 10. Time spent

> Replace with actual figures before submitting.

| phase | hours |
|---|---|
| Reading the brief, rules and seeds; repo setup | 0.5 |
| Fixture extension and hand-derived reference cases | 0.75 |
| Storage, loader, authority resolution | 1.0 |
| Model client with record/replay | 1.25 |
| Drafting, parsing, validation | 1.0 |
| Review, approval, reuse, staleness | 1.0 |
| Streamlit queue | 1.0 |
| Checks, fixing the authority-loss gap | 0.75 |
| Documentation and AI configuration manifest | 0.75 |
| **Total** | **8.0** |

---

## 11. Submission map

| requirement | where |
|---|---|
| Git repository | this repo, with incremental commit history |
| Supplied seeds unchanged | `data/starter-pack/` |
| My additions and generation method | `data/additions.json`, section 6 |
| Five checked reference cases | `data/reference-cases.json` |
| Check results, expected vs observed | `checks/report-*.txt` and `.json` |
| Setup and run instructions | sections 1 and 3 |
| Architecture | sections 2, 4, 5 |
| Model configuration | section 3, `app/config.py` |
| Data assumptions | section 8 |
| Known limitations | section 9 |
| Replay without an API key | section 3 |
| Written walkthrough | `docs/walkthrough.md` |
| LLM usage note | `notes/llm-usage.md` |
| AI configuration | `ai-workflow/manifest.json`, `ai-workflow/README.md` |

All people, businesses, documents and policies in this repository are fictional and come from
the supplied starter pack.
