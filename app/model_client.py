"""Model integration with record / replay / stub modes.

The project has to run for someone with no API key, so every live call is recorded to
cache/<sha256>.json and can be replayed later.

    MODEL_MODE=live    call the provider, record the response        (needs MODEL_API_KEY)
    MODEL_MODE=replay  serve recorded responses, never use network   (the default)
    MODEL_MODE=stub    offline keyword-overlap stand-in, no network, no cache

The cache key is a hash of everything that could change the answer: model id, temperature,
max tokens, system prompt and user prompt. If any of them changes, the old response is
not used.

The stub exists so the pipeline can be developed and the plumbing tested without
credentials. It is NOT a model and never counts as a real run: it is labelled "stub"
in the database, in the UI and in the check report. See README "Model access".
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import config


class ModelError(RuntimeError):
    """Raised when no usable response can be produced. Surfaced, never swallowed."""


class ReplayMiss(ModelError):
    pass


@dataclass
class ModelResponse:
    text: str
    mode: str
    model_id: str
    cache_key: str
    cached: bool
    latency_ms: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)


def cache_key(system: str, user: str, model_id: str, temperature: float, max_tokens: int) -> str:
    payload = json.dumps(
        {
            "model_id": model_id,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "system": system,
            "user": user,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(key: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.CACHE_DIR) / f"{key}.json"


def _write_cache(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)


# --- live ---------------------------------------------------------------------

def _call_live(
    system: str, user: str, model_id: str, temperature: float, max_tokens: int
) -> tuple[str, dict[str, Any], int]:
    api_key = os.environ.get(config.API_KEY_ENV)
    if not api_key:
        raise ModelError(
            f"MODEL_MODE=live but {config.API_KEY_ENV} is not set. "
            "Export it, or use MODEL_MODE=replay to serve recorded responses."
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ModelError(
            "MODEL_MODE=live needs the 'anthropic' package: pip install -r requirements.txt"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    started = time.monotonic()
    try:
        message = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            # anthropic 1.x dropped temperature from the create() signature; the API still
            # accepts it for this model, so it is sent in the request body directly.
            extra_body={"temperature": temperature},
        )
    except anthropic.APIError as exc:
        # Turned into a visible "error" row by the pipeline instead of stopping the batch.
        raise ModelError(f"model API call failed: {exc}") from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = {
        "input_tokens": getattr(message.usage, "input_tokens", None),
        "output_tokens": getattr(message.usage, "output_tokens", None),
    }
    return text, usage, latency_ms


# --- stub ---------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return [t for t in cleaned.split() if t and t not in config.STOPWORDS]


def _call_stub(user: str) -> str:
    """Naive keyword-overlap stand-in.

    It parses the passage block out of the prompt, scores each authoritative passage by
    token overlap with the question, and answers with the best one. It is intentionally
    simple and does not check whether the passage answers the question; catching that is
    the validator's job.
    """
    question = ""
    for line in user.splitlines():
        if line.startswith("QUESTION:"):
            question = line[len("QUESTION:") :].strip()
            break

    passages: list[dict[str, str]] = []
    for line in user.splitlines():
        line = line.strip()
        if not line.startswith("<passage "):
            continue
        pid = line.split('id="', 1)[1].split('"', 1)[0]
        authority = line.split('authority="', 1)[1].split('"', 1)[0]
        text = line.split(">", 1)[1].rsplit("</passage>", 1)[0]
        passages.append({"id": pid, "authority": authority, "text": text})

    q_tokens = set(_tokens(question))

    def overlap(p: dict[str, str]) -> int:
        return len(q_tokens & set(_tokens(p["text"])))

    authoritative = [p for p in passages if p["authority"] == "authoritative"]
    scored = sorted(authoritative, key=overlap, reverse=True)
    best = scored[0] if scored and overlap(scored[0]) > 0 else None

    if best is None:
        return json.dumps(
            {
                "status": "unresolved",
                "answer": "",
                "cited_passage_ids": [],
                "quoted_excerpts": {},
                "conflicting_passage_ids": [],
                "reason": "No supplied passage mentions this topic, so it is undocumented.",
            }
        )

    # Any superseded passage sharing vocabulary with the chosen one is reported as a conflict.
    best_tokens = set(_tokens(best["text"]))
    conflicts = [
        p["id"]
        for p in passages
        if p["authority"] == "superseded" and len(best_tokens & set(_tokens(p["text"]))) >= 2
    ]

    return json.dumps(
        {
            "status": "answered",
            "answer": best["text"],
            "cited_passage_ids": [best["id"]],
            "quoted_excerpts": {best["id"]: best["text"]},
            "conflicting_passage_ids": conflicts,
            "reason": "",
        }
    )


# --- public entry point -------------------------------------------------------

def complete(
    system: str,
    user: str,
    *,
    mode: str | None = None,
    model_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    cache_dir: Path | None = None,
) -> ModelResponse:
    mode = (mode or config.MODEL_MODE).lower()
    model_id = model_id or config.MODEL_ID
    temperature = config.MODEL_TEMPERATURE if temperature is None else temperature
    max_tokens = max_tokens or config.MODEL_MAX_TOKENS

    key = cache_key(system, user, model_id, temperature, max_tokens)
    path = _cache_path(key, cache_dir)

    if mode == "stub":
        return ModelResponse(
            text=_call_stub(user), mode="stub", model_id="stub-keyword-overlap",
            cache_key=key, cached=False,
        )

    if mode == "replay":
        if not path.exists():
            raise ReplayMiss(
                f"No recorded response for cache key {key[:12]}... in {path.parent}. "
                "Run once with MODEL_MODE=live and MODEL_API_KEY set to record it, "
                "or use MODEL_MODE=stub to develop offline."
            )
        with path.open(encoding="utf-8") as fh:
            record = json.load(fh)
        return ModelResponse(
            text=record["response"], mode="replay", model_id=record.get("model_id", model_id),
            cache_key=key, cached=True, usage=record.get("usage", {}),
        )

    if mode == "live":
        text, usage, latency_ms = _call_live(system, user, model_id, temperature, max_tokens)
        _write_cache(
            path,
            {
                "cache_key": key,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mode": "live",
                "model_id": model_id,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "system": system,
                "prompt": user,
                "response": text,
                "usage": usage,
                "latency_ms": latency_ms,
            },
        )
        return ModelResponse(
            text=text, mode="live", model_id=model_id, cache_key=key, cached=False,
            latency_ms=latency_ms, usage=usage,
        )

    raise ModelError(f"Unknown MODEL_MODE {mode!r}. Use live, replay or stub.")


def cache_inventory(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Used by the UI and the check report to show what has actually been recorded."""
    directory = cache_dir or config.CACHE_DIR
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                rec = json.load(fh)
        except json.JSONDecodeError:
            continue
        out.append(
            {
                "cache_key": rec.get("cache_key", path.stem),
                "model_id": rec.get("model_id"),
                "recorded_at": rec.get("recorded_at"),
                "mode": rec.get("mode"),
                "file": path.name,
            }
        )
    return out
