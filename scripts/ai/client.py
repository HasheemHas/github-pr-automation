"""OpenAI-compatible NVIDIA client with strict, fail-safe JSON handling.

Model IDs and the endpoint are configurable; the defaults below are the intended
production values. Repository data (diffs, titles, comments) is untrusted input
and is never treated as instructions.

The API key is read from the environment on demand and is never logged, echoed
into an error message, or written to an artifact.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Mapping

from ..common.errors import AIError

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_METADATA_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
# Review, test-gap analysis and patch generation all use this model.
DEFAULT_REVIEW_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

#: Substrings that indicate the model/provider rejected structured-output mode
#: rather than rejecting our prompt.
_RESPONSE_FORMAT_HINTS = (
    "response_format",
    "json_object",
    "json mode",
    "structured output",
    "guided_json",
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def base_url(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (environ.get("AI_BASE_URL") or "").strip() or DEFAULT_BASE_URL


def metadata_model(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (environ.get("AI_METADATA_MODEL") or "").strip() or DEFAULT_METADATA_MODEL


def review_model(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (environ.get("AI_REVIEW_MODEL") or "").strip() or DEFAULT_REVIEW_MODEL


def api_key(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    key = (environ.get("NVIDIA_API_KEY") or environ.get("AI_API_KEY") or "").strip()
    if not key:
        raise AIError("NVIDIA_API_KEY is not configured.")
    return key


def create_client(env: Mapping[str, str] | None = None):
    """Build the OpenAI-compatible client. Imported lazily to keep tests light."""
    from openai import OpenAI  # noqa: PLC0415 - optional at import time

    return OpenAI(base_url=base_url(env), api_key=api_key(env), max_retries=2)


def _looks_like_response_format_rejection(exc: Exception) -> bool:
    message = str(exc).lower()
    if any(hint in message for hint in _RESPONSE_FORMAT_HINTS):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    # A 400/422 on the very first structured attempt is worth one plain retry.
    return status in (400, 422)


def extract_json(content: str) -> dict[str, Any]:
    """Strictly parse a JSON object out of a model response.

    Tolerates a surrounding markdown fence and leading prose, but never tolerates
    malformed JSON.
    """
    if not content or not content.strip():
        raise AIError("The model returned an empty response.")
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise AIError("The model response did not contain a JSON object.") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AIError(f"The model response was not valid JSON: {exc.msg}.") from None
    if not isinstance(data, dict):
        raise AIError("The model response was valid JSON but not a JSON object.")
    return data


def complete_json(
    *,
    model: str,
    system: str,
    user: str,
    validator: Callable[[Mapping[str, Any]], dict[str, Any]],
    client=None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Ask for JSON, falling back when the model rejects structured-output mode.

    Flow: structured JSON mode -> on provider rejection retry without
    ``response_format`` -> strict parse -> schema validation. Any failure raises
    :class:`AIError` so callers can fail safe instead of writing bad output.
    """
    active = client if client is not None else create_client(env)
    attempts: list[dict[str, Any]] = [
        {"response_format": {"type": "json_object"}},
        {},  # plain-text fallback for models without structured-output support
    ]

    last_error: Exception | None = None
    for index, extra in enumerate(attempts):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if not extra:
            # Without structured mode, restate the contract in the prompt.
            messages[0]["content"] = (
                system + "\n\nRespond with a single raw JSON object and nothing else. "
                "No markdown, no code fence, no commentary."
            )
        try:
            response = active.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra,
            )
        except Exception as exc:  # provider errors vary by SDK version and model
            last_error = exc
            if index == 0 and _looks_like_response_format_rejection(exc):
                continue
            raise AIError(f"Model {model} request failed: {type(exc).__name__}.") from None

        try:
            choices = response.choices or []
            content = choices[0].message.content if choices else ""
        except (AttributeError, IndexError) as exc:  # pragma: no cover - defensive
            raise AIError(f"Model {model} returned an unreadable response.") from exc

        return validator(extract_json(content or ""))

    raise AIError(
        f"Model {model} rejected both structured and plain JSON requests: "
        f"{type(last_error).__name__ if last_error else 'unknown error'}."
    )


def list_models(client=None, env: Mapping[str, str] | None = None) -> list[str]:
    """List model IDs the endpoint advertises. Used by the diagnostic script."""
    active = client if client is not None else create_client(env)
    try:
        page = active.models.list()
    except Exception as exc:
        raise AIError(f"Could not list models: {type(exc).__name__}.") from None
    ids: list[str] = []
    for item in getattr(page, "data", None) or []:
        model_id = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
        if model_id:
            ids.append(str(model_id))
    return sorted(ids)
