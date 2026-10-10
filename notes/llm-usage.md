# LLM usage note

> **To complete before submitting:** this file records what actually happened during
> development. Check every line against your own session and adjust anything that differs —
> especially the model/tool versions and the time spent. The follow-up interview asks you to
> explain one generated module, so do not leave anything here you cannot talk through.

## 1. Tools and models

| role | tool / model | version | where |
|---|---|---|---|
| Development assistant | Claude (Anthropic), used conversationally to generate the solution | `claude-opus-5` | whole repository |
| Application model integration | Claude via the Anthropic Messages API | `claude-sonnet-4-5` (set in `app/config.py`, override with `MODEL_ID`) | `app/model_client.py` |
| Python SDK | `anthropic` | 1.13.0 | `requirements.txt` |
| UI framework | `streamlit` | 1.65.0 | `app/ui.py` |
| Runtime | CPython | 3.13.16 | — |

Application model parameters: `temperature=0`, `max_tokens=1024`. Temperature is zero so a
recorded response is a fair representative of a repeat call, which is what makes
`MODEL_MODE=replay` honest.

## 2. What was generated

All of it: fixtures, loader, storage, model client, prompts, validation, review logic,
Streamlit UI, the check harness and this documentation. The brief permits this explicitly.
What is mine is the design — the choice to resolve authority in code rather than in the
prompt, the five-check validation layer, the approval version snapshot — and the
verification below.

## 3. One representative instruction

The instruction that shaped the architecture, paraphrased:

> Authority between documents is decided by the `supersedes` field, which is deterministic.
> Do not ask the model to work out which document wins. Resolve it in the loader, store
> `is_authoritative` on each document, and hand the model passages that are already labelled
> authoritative or superseded. The model's job is to answer and cite; the application's job
> is to decide what counts as evidence.

This is why `app/loader.py` owns `resolve_authority()` and why the prompt in
`app/drafting.py` carries `authority="..."` on every `<passage>` tag.

A second instruction worth recording, about prompt construction:

> Keep instructions in the system prompt and document text in the user message inside
> `<passage>` tags. Document content is data, not instructions.

## 4. One correction, with how it was caught

**Caught by:** running `scripts/bump_source_version.py --mode supersede` and reading the
output, rather than assuming the mode worked because `--mode bump` did.

**What was wrong.** The staleness check compared the approved answer's source-version
snapshot against current document versions. The `supersede` path inserts `EXPORT-v3`, which
supersedes `EXPORT-v2`, but leaves `EXPORT-v2` at version 2. The snapshot said
`{"EXPORT-v2": 2}` and the live version was still 2, so **nothing was flagged** — yet the
approved answer was now resting on a passage that rule 2 forbids answering from.

Observed output before the fix:

```json
{ "action": "supersede", "inserted": "EXPORT-v3",
  "now_superseded": "EXPORT-v2", "flagged_needs_review": [] }
```

**The fix.** Added `cited_authority_lost()` and `approval_is_stale()` in `app/review.py`, so
an approval goes stale for either reason: a cited document's version moved, **or** a cited
passage lost authority. `refresh_source_change_flags()` and `find_reusable()` both call
`approval_is_stale()` now.

After the fix:

```json
{ "action": "supersede", "inserted": "EXPORT-v3", "now_superseded": "EXPORT-v2",
  "flagged_needs_review": [ { "question_id": "Q1", "version_changed": {},
    "authority_lost": [ { "passage_id": "EXPORT-v2:p1",
      "problem": "EXPORT-v2 is now superseded by EXPORT-v3" } ] } ] }
```

The gap is recorded as assumption 2 in `README.md`, because the supplied rules do not say
what should happen in that case and I had to decide.

## 5. Other verification performed

- **Hand-derived expected values first.** `data/reference-cases.json` was written by reading
  the passages, before the pipeline could produce anything, so the application's answers were
  never used as the answer key.
- **Ran the whole queue and compared every row** against the hand derivation: `Q1` →
  `EXPORT-v2:p1` with the `EXPORT-v1:p1` conflict, `Q2` → unresolved on `json`, `Q11` →
  unresolved on uncovered `free`/`plan`, `Q9` → `RETENTION-v2:p1` with its conflict.
- **Rendered the Streamlit UI in a headless browser** rather than trusting that the server
  returned HTTP 200. The first render failed with `ModuleNotFoundError: No module named
  'app'`, because `streamlit run app/ui.py` puts `app/` on `sys.path` and not the repository
  root. Fixed by inserting the repository root in `app/ui.py`.
- **Checked the stoplist against every question.** `Q9` ("How **long** are deleted projects
  retained?") was initially routed as undocumented because `long` appears in no passage.
  `long`, `often`, `many`, `much`, `far` and `soon` were added to the stoplist as generic
  English question-form words. They carry no product meaning, so this adds no domain rule.
- **Each reference case runs against a throwaway database**, so a passing run cannot depend
  on state left behind by an earlier case.

## 6. What I would not claim

The committed check report was produced with `MODEL_MODE=stub`, an offline keyword stand-in,
not a language model. It demonstrates that the pipeline, validation and review logic behave
as derived by hand; it demonstrates nothing about model quality. A `live` run and a `replay`
re-run must replace it before this is submitted (README section 3).

Five fictional documents and thirteen questions are a demonstration set. They say nothing
about accuracy on other data.
