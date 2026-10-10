# AI workflow used during this exercise

Companion to `manifest.json`. Both describe the AI configuration for the Alternative C
take-home; the application itself is documented in the repository `README.md`.

## Tools and models

**Development time.** Claude (`claude-opus-5`) used conversationally to generate the
solution — fixtures, application code, the check harness and the documentation. The brief
permits generating the whole solution, so the work that matters is the design decisions and
the verification, both recorded in `notes/llm-usage.md`. No editor plugin, agent framework,
subagent or MCP server was involved.

**Application runtime.** A single Anthropic Messages API call per question, through the
`anthropic` Python SDK (1.13.0).

| setting | value | where |
|---|---|---|
| model id | `claude-sonnet-4-5` | `app/config.py :: MODEL_ID`, override `MODEL_ID` |
| temperature | `0` | `app/config.py :: MODEL_TEMPERATURE` |
| max tokens | `1024` | `app/config.py :: MODEL_MAX_TOKENS` |
| mode | `live` / `replay` / `stub` | `app/config.py :: MODEL_MODE` |
| cache directory | `cache/` | `app/config.py :: CACHE_DIR` |

Temperature is 0 deliberately: a recorded response is only a fair stand-in for a repeat call
if the call is as close to deterministic as the provider allows.

No tool calling, no streaming, no fine-tuning, no multi-agent orchestration. The brief lists
all of those as optional, and none of them would make this task's required flow work better.

## Configuration files

Everything that affects behaviour is in version control at its normal location. Nothing is a
sanitized snapshot, because no user-level or machine-level configuration was involved.

| component | path | status |
|---|---|---|
| Application system prompt and prompt builder | `app/drafting.py` | used |
| Model parameters and mode switch | `app/config.py` | used |
| Model transport, cache, record/replay | `app/model_client.py` | used |
| Recorded real responses | `cache/*.json` | used |
| Environment variable names | `ai-workflow/.env.example` | used |
| CLAUDE.md / agent instructions | — | not-used |
| Subagent definitions | — | not-used |
| Hooks and hook scripts | — | not-used |
| Custom skills | — | not-used |
| MCP / tool settings | — | not-used |

**There are no hooks in this repository**, so there is nothing for a reviewer to read and
enable before running anything here.

The starter pack's `skills/generate-assignment-data/SKILL.md` was read but not installed or
run. Fixtures were hand-written instead, so each addition could be tied to the specific
check it serves — see the `_why` field on every entry in `data/additions.json`. No custom
skill, agent or hook was created to fill out this manifest.

## One workflow example

Summarised here; the full version with before-and-after output is in `notes/llm-usage.md`.

**Instruction.** Resolve document authority in the loader rather than in the prompt. The
`supersedes` field is deterministic, so the model should receive passages already labelled
authoritative or superseded, and should only answer and cite.

**Which configuration it affected.** `app/drafting.py :: SYSTEM_PROMPT` (rule 3 of the
prompt: never answer from a passage marked `authority="superseded"`) and the
`<passage authority="...">` tags built by `build_user_prompt()`.

**How the result was checked.** The five reference cases in `data/reference-cases.json`,
whose expected values were hand-derived from the passages before the pipeline could produce
anything. RC-3 specifically asserts that `Q1` cites `EXPORT-v2:p1` and reports
`EXPORT-v1:p1` as a conflict.

**One correction.** Running `scripts/bump_source_version.py --mode supersede` showed
`"flagged_needs_review": []` when it should have flagged `Q1`: the staleness check only
compared version numbers, and inserting `EXPORT-v3` leaves `EXPORT-v2` at version 2 while
stripping its authority. `cited_authority_lost()` and `approval_is_stale()` were added to
`app/review.py` so an approval goes stale for either reason. The gap is recorded as
assumption 2 in the repository `README.md`, since the supplied rules do not cover that case.

## Reproduce or replay

Configuration stays where it is; nothing needs installing into a personal tool directory.

```bash
pip install -r requirements.txt          # Python 3.11+, developed on 3.13.16
python -m app.loader --reset

# No API key needed - serves the responses recorded in cache/
MODEL_MODE=replay python checks/run_checks.py --json checks/report-replay.json
MODEL_MODE=replay streamlit run app/ui.py

# Offline, no credentials and no cache needed - keyword stand-in, clearly labelled
MODEL_MODE=stub python checks/run_checks.py
```

To record fresh responses:

```bash
export MODEL_API_KEY=...
MODEL_MODE=live python -c "from app import storage,pipeline; pipeline.draft_all(storage.connect(), force=True)"
```

Every live call writes `cache/<sha256>.json` containing the model id, parameters, system
prompt, user prompt, response and token usage. The hash covers everything that could change
the answer, so editing the prompt produces a cache miss rather than a stale replay.

## Decisions and limitations

**Why this setup suited the task.** The assignment is about whether an answer can be proved
against its evidence, and that work is deterministic: authority resolution, citation
existence, verbatim excerpt matching, version snapshots. Only the drafting step needs a
model. A default, minimal setup leaves the interesting logic in reviewable Python rather
than spread across agent configuration.

**What I would change with more time.** Replace the lexical coverage check with an
embedding or entailment-based one and keep the lexical test as a cheap pre-filter; add a
second-pass judge that checks the answer prose is entailed by the quoted excerpt, with its
own evaluation before being trusted; add a regression suite over recorded responses so
prompt edits can be diffed against previous behaviour.

**Honest limitation.** The committed check report was produced with `MODEL_MODE=stub`. It
shows the pipeline and review logic behave as hand-derived; it shows nothing about model
quality. A `live` run and a `replay` re-run must replace it before submission.
