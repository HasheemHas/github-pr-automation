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

from ..common.errors import AIError, SchemaError
from .telemetry import Recorder, usage_of

#: Output-token budget. Reasoning models spend tokens before answering, so this
#: needs headroom over the size of the JSON itself.
DEFAULT_MAX_TOKENS = 8192
#: Characters of a bad response quoted back in an error message.
PREVIEW_CHARS = 300

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_METADATA_MODEL = "poolside/laguna-xs-2.1"
# Review, test-gap analysis and patch generation all use this model.
DEFAULT_REVIEW_MODEL = "poolside/laguna-xs-2.1"

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
#: Reasoning blocks emitted before the real answer.
_THINK = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE
)


def base_url(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (environ.get("AI_BASE_URL") or "").strip() or DEFAULT_BASE_URL


def metadata_model(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (environ.get("AI_METADATA_MODEL") or "").strip() or DEFAULT_METADATA_MODEL


def review_model(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (environ.get("AI_REVIEW_MODEL") or "").strip() or DEFAULT_REVIEW_MODEL


def max_output_tokens(env: Mapping[str, str] | None = None) -> int:
    """Output-token budget, overridable with the ``AI_MAX_TOKENS`` variable."""
    environ = os.environ if env is None else env
    raw = (environ.get("AI_MAX_TOKENS") or "").strip()
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


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


def strip_reasoning(text: str) -> str:
    """Remove a reasoning model's thinking block from its answer.

    Reasoning-capable models often prepend ``<think>…</think>`` (or similar) before
    the real answer. Left in place, the JSON extractor sees prose and gives up.
    """
    cleaned = _THINK.sub("", text or "")
    # An unterminated block means the model never got past thinking; keep only what
    # follows the opening tag so the caller sees an empty answer rather than prose.
    for opener in ("<think>", "<thinking>", "<reasoning>"):
        if opener in cleaned and cleaned.rindex(opener) > -1:
            head, _, tail = cleaned.partition(opener)
            cleaned = head if "{" in head else tail
    return cleaned.strip()


def _unterminated_reasoning(text: str) -> bool:
    """True when a reasoning block was opened and never closed."""
    lowered = (text or "").lower()
    return any(
        lowered.count(f"<{tag}>") > lowered.count(f"</{tag}>")
        for tag in ("think", "thinking", "reasoning")
    )


def preview(content: str, limit: int = PREVIEW_CHARS) -> str:
    """A short, single-line excerpt of a model response, for error messages."""
    text = re.sub(r"\s+", " ", (content or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def extract_json(content: str) -> dict[str, Any]:
    """Strictly parse a JSON object out of a model response.

    Tolerates a markdown fence, leading prose and a reasoning block, but never
    tolerates malformed JSON. Failures quote what came back so the run log
    explains itself instead of just saying "no JSON".
    """
    if not content or not content.strip():
        raise AIError("The model returned an empty response.")
    raw = content.strip()
    text = strip_reasoning(raw)
    if not text or ("{" not in text and _unterminated_reasoning(raw)):
        # The model spent its whole answer thinking and never reached the JSON.
        raise AIError(
            "The model returned only a reasoning block and no answer. "
            "Raise the AI_MAX_TOKENS variable so it has room to emit the JSON."
        )
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise AIError(
                "The model response did not contain a JSON object. It began: "
                f"{preview(text)!r}"
            ) from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AIError(
                f"The model response was not valid JSON: {exc.msg}. It began: "
                f"{preview(text)!r}"
            ) from None
    if not isinstance(data, dict):
        raise AIError(
            f"The model response was valid JSON but not an object: {preview(text)!r}"
        )
    return data


def _message_text(response) -> tuple[str, str]:
    """Return ``(content, finish_reason)``, falling back to a reasoning field.

    Some reasoning models put the answer in ``reasoning_content`` and leave
    ``content`` empty. Treating that as an empty response loses a usable answer.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "", ""
    choice = choices[0]
    finish = str(getattr(choice, "finish_reason", "") or "")
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) or ""
    if not str(content).strip():
        for field in ("reasoning_content", "reasoning"):
            alternative = getattr(message, field, None)
            if alternative and str(alternative).strip():
                return str(alternative), finish
    return str(content), finish


def complete_json(
    *,
    model: str,
    system: str,
    user: str,
    validator: Callable[[Mapping[str, Any]], dict[str, Any]],
    client=None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    env: Mapping[str, str] | None = None,
    recorder: "Recorder | None" = None,
    section: str = "",
) -> dict[str, Any]:
    """Ask for JSON, escalating strictness until the model complies.

    Flow: structured JSON mode -> plain with the contract restated -> plain with a
    blunt "output only JSON" instruction. A reasoning block is stripped, an empty
    ``content`` falls back to ``reasoning_content``, and running out of output
    tokens is reported as exactly that. Any final failure raises :class:`AIError`
    so callers fail safe instead of writing bad output.
    """
    active = client if client is not None else create_client(env)
    budget = max_tokens or max_output_tokens(env)

    #: (extra kwargs, extra system instruction). Escalating strictness: a parse
    #: failure is retried with a blunter instruction before giving up.
    attempts: list[tuple[dict[str, Any], str]] = [
        ({"response_format": {"type": "json_object"}}, ""),
        (
            {},
            "\n\nRespond with a single raw JSON object and nothing else. "
            "No markdown, no code fence, no commentary.",
        ),
        (
            {},
            "\n\nOutput ONLY a JSON object. Your first character must be '{' and your "
            "last must be '}'. Do not think out loud, do not explain, do not use "
            "markdown. If you are unsure of a field, use an empty string or an empty "
            "list rather than omitting the object.",
        ),
    ]

    total = len(attempts)
    last_error: AIError | None = None
    for index, (extra, suffix) in enumerate(attempts):
        is_last = index == total - 1
        system_text = system + suffix
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user},
        ]

        def record(outcome: str, *, response_text: str = "", finish: str = "",
                   usage: Mapping[str, Any] | None = None, error: str = "") -> None:
            """Log this attempt, whatever happened to it."""
            if recorder is None:
                return
            entry = recorder.add(
                section=section or "ai",
                model=model,
                attempt=index + 1,
                attempts_total=total,
                structured=bool(extra),
                outcome=outcome,
                started=started,
                system=system_text,
                prompt=user,
                response_text=response_text,
                finish_reason=finish,
                usage=usage,
                max_tokens=budget,
                error=error,
            )
            print(recorder.line(entry))

        started = recorder.start() if recorder is not None else 0.0
        try:
            response = active.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=budget,
                **extra,
            )
        except Exception as exc:  # provider errors vary by SDK version and model
            record("provider_error", error=f"{type(exc).__name__}: {exc}")
            if index == 0 and _looks_like_response_format_rejection(exc):
                last_error = AIError(f"structured mode rejected: {type(exc).__name__}")
                continue
            raise AIError(f"Model {model} request failed: {type(exc).__name__}.") from None

        content, finish_reason = _message_text(response)
        counts = usage_of(response)

        if finish_reason == "length" and "{" not in content:
            # Out of tokens before producing anything parseable: say so plainly
            # rather than reporting "no JSON object".
            error = AIError(
                f"Model {model} hit the {budget}-token output limit before emitting "
                "JSON. Raise the AI_MAX_TOKENS variable."
            )
            record("truncated", response_text=content, finish=finish_reason,
                   usage=counts, error=str(error))
            if is_last:
                raise error
            last_error = error
            continue

        try:
            result = validator(extract_json(content))
        except AIError as exc:
            outcome = "schema_error" if isinstance(exc, SchemaError) else (
                "empty" if "empty response" in str(exc) else "parse_error"
            )
            record(outcome, response_text=content, finish=finish_reason,
                   usage=counts, error=str(exc))
            # A parse or schema failure is worth one stricter retry; the same model
            # asked more bluntly often complies.
            if is_last:
                raise
            last_error = exc
            continue

        record("ok", response_text=content, finish=finish_reason, usage=counts)
        return result

    raise AIError(
        f"Model {model} did not return usable JSON after {total} attempts. "
        f"Last error: {last_error}"
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
