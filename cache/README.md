# Recorded model responses

Every `MODEL_MODE=live` call writes one file here, named `<sha256>.json`, containing the
model id, parameters, system prompt, user prompt, response text and token usage.

`MODEL_MODE=replay` serves these files and never touches the network, which is how a
reviewer runs the project and the five reference cases without an API key.

The hash covers the model id, temperature, max tokens, system prompt and user prompt, so
after editing any of them the old response is not used.

These files contain no credentials: the API key is read from the environment and never
written to disk.

The committed files are 12 real `claude-sonnet-4-5` responses recorded on 2026-10-10 for the
13 questions (`Q13` has the same text as `Q1`, so it shares one response). See README section 3.
