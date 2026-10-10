# LLM usage note

This note lists the AI tools used, what they generated, what I did, and the corrections
made along the way, with how each was caught.

## 1. Tools and models

| role | tool / model | version | where |
|---|---|---|---|
| Development assistant | Claude on the web (claude.ai) | Claude Opus 5 and Claude Sonnet 5, chosen per task | design discussion, fixtures, first version of the code and documentation |
| Development assistant | Claude Code, VS Code extension | extension 2.1.294; Claude Opus 5.5 (user setting `model: opus`, which resolves to the current Opus; extended thinking always on) | running the project, the live model run, debugging, the final review against the brief, documentation updates |
| Application model integration | Claude via the Anthropic Messages API | `claude-sonnet-4-5` (set in `app/config.py`, override with `MODEL_ID`) | `app/model_client.py` |
| Python SDK | `anthropic` | 1.13.0 | `requirements.txt` |
| UI framework | `streamlit` | 1.65.0 | `app/ui.py` |
| Runtime | CPython | 3.12.11 for the final live run and all checks | — |

Application model parameters: `temperature=0`, `max_tokens=1024`. Temperature is zero so a
new call would almost always return the same answer as the recorded one, which is what
replay relies on. The application uses no thinking, tools or streaming.

## 2. What was generated and what I did

**Generated with AI assistance:** the fixture additions, the application code (loader,
storage, model client, prompts, validation, review logic, Streamlit UI), the check
harnesses and the first drafts of the documentation.

**My part:**

- Read the brief and starter pack, chose Alternative C and kept the work inside its scope.
- Directed the build, including the instructions in section 3 that shaped the design.
- Set up model access (an Anthropic API key with its own credit) and ran the live model
  calls that produced `cache/`. The first live attempts failed twice; I brought the errors
  back to Claude Code and worked through the fixes with it (section 4).
- Checked results against the supplied `expected-seed-results.json` and the hand-derived
  reference cases, never against the application's own output.
- Asked for the finished submission to be reviewed against the brief's requirement list
  with Claude Code, and decided which findings to act on (section 5).

## 3. One representative instruction

The instruction that shaped the architecture, paraphrased:

> Authority between documents is decided by the `supersedes` field, which is deterministic.
> Do not ask the model to work out which document wins. Resolve it in the loader, store
> `is_authoritative` on each document, and hand the model passages that are already labelled
> authoritative or superseded. The model's job is to answer and cite; the application's job
> is to decide what counts as evidence.

This is why `app/loader.py` owns `resolve_authority()` and why the prompt in
`app/drafting.py` carries `authority="..."` on every `<passage>` tag.

A second instruction, about prompt construction:

> Keep instructions in the system prompt and document text in the user message inside
> `<passage>` tags. Document content is data, not instructions.

## 4. Corrections, with how they were caught

### 4a. The first live run failed: generated code did not match the installed SDK

**Caught by:** running the application against the real API for the first time, instead of
relying on the offline stub that had passed every check until then.

**What was wrong.** Two problems in `app/model_client.py`:

1. It passed `temperature=0` as a keyword argument to `client.messages.create()`. The
   installed `anthropic` 1.13.0 SDK removed sampling parameters from that signature, so every
   live call raised `TypeError: Messages.create() got an unexpected keyword argument
   'temperature'`. The stub never reached this code, so the bug was invisible offline.
2. Only a missing key was turned into a `ModelError`. Any API error escaped as an exception
   and would have stopped the batch instead of producing a visible `error` row.

**The fix.** `temperature` is now sent in the request body with `extra_body`, since the API
still accepts it for `claude-sonnet-4-5` and the replay design depends on temperature 0.
Every `anthropic.APIError` is now wrapped in `ModelError`, which the pipeline already turns
into an `error` row.

**How the fix was checked.** The next live attempt proved the error handling with a real
failure: the API key's account had no credit, every call returned HTTP 400, and all 13
questions were stored as `error` with the provider's message while the batch completed.
That run also showed that a fully failed run printed nothing, so `python -m app.pipeline`
was added to print the status counts and exit non-zero when any question errors. After
credit was added, the live run recorded 12 responses, and the reference cases passed 5/5 in
replay (`checks/report-replay.txt`).

### 4b. The staleness check missed a superseded source

**Caught by:** running `scripts/bump_source_version.py --mode supersede` during development
and reading its output, rather than assuming the mode worked because `--mode bump` did.

**What was wrong.** The staleness check compared the approved answer's source-version
snapshot against current document versions. The `supersede` path inserts `EXPORT-v3`, which
supersedes `EXPORT-v2`, but leaves `EXPORT-v2` at version 2, so nothing was flagged, yet
the approved answer was now resting on a passage that rule 2 forbids answering from.

```json
{ "action": "supersede", "inserted": "EXPORT-v3",
  "now_superseded": "EXPORT-v2", "flagged_needs_review": [] }
```

**The fix.** `cited_authority_lost()` and `approval_is_stale()` in `app/review.py`, so an
approval goes stale for either reason: a cited document's version moved, or a cited passage
lost authority. Recorded as assumption 2 in `README.md`, because the supplied rules do not
cover this case.

## 5. Other verification performed

- **Expected values written before the pipeline produced anything.**
  `data/reference-cases.json` was derived from the passages by hand, so the application's
  answers were never used as the answer key.
- **Real answers compared with the supplied expected results.** `Q1` answers "no" from
  `EXPORT-v2:p1` with the `EXPORT-v1:p1` conflict shown; `Q2` stays unresolved and is routed
  to the Product reviewer; `Q3` gives Monday to Friday, 09:00 to 17:00 UTC.
- **The rest of the recorded queue checked by reading it.** `Q10` (undocumented response
  time) and `Q11` (plan tiers not covered by the invite passage) stay unresolved; `Q9` uses
  `RETENTION-v2` (30 days), not the superseded 90 days.
- **Failure handling demonstrated with labelled simulations**: API failure mid-batch,
  non-JSON output, a non-existent citation, a paraphrased quotation, an answer built on a
  superseded passage, and a conflict between two current documents
  (`checks/report-failures.txt`).
- **The UI was tested.** The Streamlit page was run headlessly with
  Streamlit's test harness on a copy of the database: it rendered without exceptions, an
  approval was recorded, and the approval was still there after a fresh load.
- **A final review against the brief, with edge cases tried on a scratch database.** It
  found four bugs: an answer whose evidence had been superseded could be approved and then
  flipped straight back to `needs_review` with a duplicated warning; re-approving after a
  version change left the old warning on an approved answer; "Draft all unanswered"
  re-drafted answers that already existed, which in live mode means paying for calls again;
  and the UI's "verified verbatim" label used a looser test than the validator. All four
  were fixed and re-tried. It also found that a conflict between two *current* documents was
  not handled, although the brief asks for it to be left for review (now check C2, shown by
  the simulated case F-6), and that the fixtures had added a reviewer for the new
  `retention` topic, which rule 4 does not allow (removed; the topic is reported as
  unmapped instead).
- **No credentials in the repository.** The recorded responses and reports were searched for
  the API key prefix before committing.

## 6. What the checks do not show

The checks show that the rules are implemented and that one recorded run of
`claude-sonnet-4-5` behaves as derived by hand. Seven short fictional documents and thirteen
questions say nothing about accuracy on other data, and a single run at temperature 0 does
not measure run-to-run variation.
