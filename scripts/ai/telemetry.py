"""Per-request logging for the AI analysis job.

Every call to the model is recorded: which section, which attempt, whether
structured output was used, prompt and response sizes, token usage, latency,
finish reason and outcome. The records go to the Actions log, to the job summary,
and to an ``ai-log.jsonl`` artifact.

Deliberately never recorded: the API key, and (unless ``AI_LOG_PROMPTS`` is set)
the prompt text. Response text is kept only as a short preview, and only when the
attempt failed — a successful response is already visible in ``analysis.json``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

#: Characters of prompt/response text kept when logging is turned up.
FULL_TEXT_LIMIT = 20000
PREVIEW_LIMIT = 500

OUTCOMES = ("ok", "provider_error", "truncated", "parse_error", "schema_error", "empty")


@dataclass
class RequestRecord:
    """One model request."""

    section: str
    model: str
    attempt: int
    attempts_total: int
    structured: bool
    outcome: str
    duration_ms: int = 0
    system_chars: int = 0
    prompt_chars: int = 0
    response_chars: int = 0
    finish_reason: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    max_tokens: int | None = None
    error: str = ""
    response_preview: str = ""
    system: str = ""
    prompt: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_of(response: Any) -> dict[str, int | None]:
    """Extract token usage from a provider response, tolerating its absence."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    return {
        "prompt_tokens": _int_or_none(getattr(usage, "prompt_tokens", None)),
        "completion_tokens": _int_or_none(getattr(usage, "completion_tokens", None)),
        "total_tokens": _int_or_none(getattr(usage, "total_tokens", None)),
    }


@dataclass
class Recorder:
    """Collects request records for one analysis run."""

    log_prompts: bool = False
    clock: Any = time.monotonic
    records: list[RequestRecord] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Recorder":
        environ = os.environ if env is None else env
        flag = (environ.get("AI_LOG_PROMPTS") or "").strip().lower()
        return cls(log_prompts=flag in {"1", "true", "yes", "on"})

    def start(self) -> float:
        return float(self.clock())

    def add(
        self,
        *,
        section: str,
        model: str,
        attempt: int,
        attempts_total: int,
        structured: bool,
        outcome: str,
        started: float,
        system: str = "",
        prompt: str = "",
        response_text: str = "",
        finish_reason: str = "",
        usage: Mapping[str, int | None] | None = None,
        max_tokens: int | None = None,
        error: str = "",
    ) -> RequestRecord:
        counts = usage or {}
        record = RequestRecord(
            section=section,
            model=model,
            attempt=attempt,
            attempts_total=attempts_total,
            structured=structured,
            outcome=outcome,
            duration_ms=int(max(0.0, float(self.clock()) - started) * 1000),
            system_chars=len(system or ""),
            prompt_chars=len(prompt or ""),
            response_chars=len(response_text or ""),
            finish_reason=finish_reason or "",
            prompt_tokens=counts.get("prompt_tokens"),
            completion_tokens=counts.get("completion_tokens"),
            total_tokens=counts.get("total_tokens"),
            max_tokens=max_tokens,
            error=(error or "")[:PREVIEW_LIMIT],
            # A failed response is quoted so the log explains itself; a successful
            # one is already in analysis.json.
            response_preview="" if outcome == "ok" else (response_text or "")[:PREVIEW_LIMIT],
            system=(system or "")[:FULL_TEXT_LIMIT] if self.log_prompts else "",
            prompt=(prompt or "")[:FULL_TEXT_LIMIT] if self.log_prompts else "",
        )
        self.records.append(record)
        return record

    # -- reporting ---------------------------------------------------------

    def line(self, record: RequestRecord) -> str:
        """A single log line for the Actions console."""
        bits = [
            f"{record.section}",
            f"attempt {record.attempt}/{record.attempts_total}",
            f"model={record.model}",
            f"mode={'json_object' if record.structured else 'plain'}",
            f"prompt={record.prompt_chars}c",
            f"response={record.response_chars}c",
            f"{record.duration_ms}ms",
        ]
        if record.completion_tokens is not None:
            bits.append(f"tokens={record.prompt_tokens}->{record.completion_tokens}")
        if record.finish_reason:
            bits.append(f"finish={record.finish_reason}")
        bits.append(f"outcome={record.outcome}")
        if record.error:
            bits.append(f"error={record.error[:200]}")
        return " | ".join(bits)

    @property
    def totals(self) -> dict[str, Any]:
        completion = [r.completion_tokens for r in self.records if r.completion_tokens is not None]
        prompt = [r.prompt_tokens for r in self.records if r.prompt_tokens is not None]
        return {
            "requests": len(self.records),
            "succeeded": sum(1 for r in self.records if r.ok),
            "failed": sum(1 for r in self.records if not r.ok),
            "retries": sum(1 for r in self.records if r.attempt > 1),
            "duration_ms": sum(r.duration_ms for r in self.records),
            "prompt_tokens": sum(prompt) if prompt else None,
            "completion_tokens": sum(completion) if completion else None,
        }

    def rows(self) -> list[list[Any]]:
        """Table rows for the job summary."""
        return [
            [
                r.section,
                f"{r.attempt}/{r.attempts_total}",
                "json_object" if r.structured else "plain",
                r.outcome,
                f"{r.duration_ms} ms",
                r.prompt_chars,
                r.response_chars,
                "—" if r.completion_tokens is None else r.completion_tokens,
                r.finish_reason or "—",
                r.error[:120],
            ]
            for r in self.records
        ]

    HEADERS = (
        "Section", "Attempt", "Mode", "Outcome", "Latency",
        "Prompt (chars)", "Response (chars)", "Out tokens", "Finish", "Error",
    )

    def write(self, path: str | Path) -> None:
        """Write one JSON object per request."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in self.records:
                payload = {k: v for k, v in asdict(record).items() if v not in ("", None)}
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
