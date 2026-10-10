# AI workflow used during this exercise

Companion to `manifest.json`. Both describe the AI configuration for the Alternative C
take-home; the application itself is documented in the repository `README.md`, and how the
tools were used is in `notes/llm-usage.md`.

## Tools and models

**Development time.** Two Claude surfaces:

| tool | version | used for |
|---|---|---|
| Claude on the web (claude.ai) | Claude Opus 5 and Claude Sonnet 5, chosen per task; the web app exposes no version | design discussion, fixture additions, first version of the code and documentation |
| Claude Code, VS Code extension | 2.1.294, Claude Opus 5.5 | running the project, the live model run, debugging the SDK mismatch, the final review against the brief, the failure-handling checks, documentation updates |

Claude Code ran with the user-level settings `model: opus` (which resolves to the current
Opus, Claude Opus 5.5) and extended thinking always on
(sanitized copy in `ai-workflow/claude-code/user-settings.sanitized.json`). Its bundled
`claude-api` skill was loaded once, to confirm that the `anthropic` 1.x SDK removed
`temperature` from `messages.create()` before the fix in `app/model_client.py` was written.
No subagent, hook, MCP server or custom skill affected the exercise.

**Application runtime.** A single Anthropic Messages API call per question, through the
`anthropic` Python SDK (1.13.0).

| setting | value | where |
|---|---|---|
| model id | `claude-sonnet-4-5` | `app/config.py :: MODEL_ID`, override `MODEL_ID` |
| temperature | `0`, sent with `extra_body` | `app/config.py :: MODEL_TEMPERATURE`, `app/model_client.py` |
| max tokens | `1024` | `app/config.py :: MODEL_MAX_TOKENS` |
| mode | `live` / `replay` / `stub` | `app/config.py :: MODEL_MODE` |
| cache directory | `cache/` | `app/config.py :: CACHE_DIR` |

Temperature is 0 so that a new call would almost always return the same answer as the
recorded one, which is what replay relies on. `anthropic` 1.x no longer
accepts `temperature` as a keyword argument, so it is passed in the request body; the API
still accepts it for this model.

`claude-sonnet-4-5` reaches end-of-life on 30 November 2026. Replay is unaffected; a new live
run after that date needs `MODEL_ID` set to a current model.

No tool calling, no streaming, no fine-tuning, no multi-agent orchestration. The brief lists
all of those as optional, and none of them would make this task's required flow work better.

## Configuration files

| component | path | status |
|---|---|---|
| Application system prompt and prompt builder | `app/drafting.py` | used |
| Model parameters and mode switch | `app/config.py` | used |
| Model transport, cache, record/replay | `app/model_client.py` | used |
| Recorded real responses | `cache/*.json` | used |
| Environment variable names | `ai-workflow/.env.example` | used |
| Claude Code user settings (model, thinking) | `~/.claude/settings.json` → `ai-workflow/claude-code/user-settings.sanitized.json` | used, redacted |
| Claude Code built-in `claude-api` skill | bundled with the extension; not a repository file | used (default, unmodified) |
| Claude on the web settings | account-level, not exportable | not-exportable |
| CLAUDE.md / project agent instructions | — | not-used |
| Subagent definitions | — | not-used |
| Hooks and hook scripts | — | not-used |
| Custom skills | — | not-used |
| MCP / tool settings | — | not-used |
| Claude Code permissions | extension defaults; each command was approved when prompted | default |

**There are no hooks in this repository**, so there is nothing for a reviewer to read and
enable before running anything here. There is no project-level `.claude/` directory.

The starter pack's `skills/generate-assignment-data/SKILL.md` was read but not installed or
run. Fixtures were written so each addition could be tied to the specific check it serves:
see the `_why` field on every entry in `data/additions.json`.

## One workflow example

Full version with output in `notes/llm-usage.md`, section 4a.

**Instruction.** Run the application against the real model for the first time:
`MODEL_MODE=live`, every question, recording each response to `cache/`.

**Which configuration it affected.** `app/model_client.py :: _call_live()` and the model
parameters in `app/config.py`.

**How the result was checked.** The live run crashed with
`TypeError: Messages.create() got an unexpected keyword argument 'temperature'`. The
generated code had been written for an older SDK, and the offline stub had never reached it.

**One correction.** `temperature` moved into `extra_body`, and API errors are now wrapped in
`ModelError` so they become visible `error` rows instead of stopping the batch. The next
run proved this with a real failure (no API credit: 13 visible `error` rows, batch
completed). After credit was added, 12 responses were recorded and the reference cases
passed 5/5 in replay.

## Reproduce or replay

Configuration stays where it is; nothing needs installing into a personal tool directory.

```bash
pip install -r requirements.txt          # Python 3.11+
python -m app.loader --reset

# No API key needed - serves the responses recorded in cache/
MODEL_MODE=replay python -m app.pipeline
MODEL_MODE=replay python checks/run_checks.py --json checks/report-replay.json
python checks/run_failure_checks.py --json checks/report-failures.json
MODEL_MODE=replay streamlit run app/ui.py
```

To record fresh responses:

```bash
export MODEL_API_KEY=...
MODEL_MODE=live python -m app.pipeline --force
```

Every live call writes `cache/<sha256>.json` containing the model id, parameters, system
prompt, user prompt, response and token usage. The hash covers everything that could change
the answer, so after a prompt edit the old response is not used.

To restore the Claude Code setup used here: install the Claude Code VS Code extension and
put the two keys from `ai-workflow/claude-code/user-settings.sanitized.json` (without the
`_` comment keys) into `~/.claude/settings.json`. Nothing else is required.

## Decisions and limitations

**Why this setup suited the task.** The assignment is about checking each answer against
its evidence, and that work can be done exactly in code: authority resolution, citation
existence, verbatim excerpt matching, version snapshots. Only the drafting step needs a
model. A default, minimal tool setup kept the important logic in plain Python that is easy
to review, instead of spreading it across agent configuration.

**What I would change.** Make one live call part of the development loop from the start: the
SDK mismatch was only found at the end because every earlier check used the offline stub.
Beyond that: replace the lexical coverage check with an embedding or entailment-based one
and keep the lexical test as a pre-filter, and add a second-pass judge that checks the
answer prose is entailed by the quoted excerpt, with its own evaluation before being trusted.
