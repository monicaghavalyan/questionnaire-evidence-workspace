# Recorded model responses

Every `MODEL_MODE=live` call writes one file here, named `<sha256>.json`, containing the
model id, parameters, system prompt, user prompt, response text and token usage.

`MODEL_MODE=replay` serves these files and never touches the network, which is how a
reviewer runs the project and the five reference cases without an API key.

The hash covers the model id, temperature, max tokens, system prompt and user prompt, so
editing any of them produces a cache miss rather than a stale response being reused quietly.

These files contain no credentials: the API key is read from the environment and never
written to disk.

**This directory is empty until a live run has been made.** See README section 3.
