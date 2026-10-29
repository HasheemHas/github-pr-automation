"""Optional model layer over the deterministic analysis.

The deterministic rules always run and always produce a complete
``analysis.json``. When ``NVIDIA_API_KEY`` is configured, this module asks the
model for two things on top of them:

* **Metadata prose** — title, summary, description, review focus and rollback.
  Measured fields (risk, breaking change, migrations, test plan) are left to the
  rules and the insights, so the description cannot contradict the checks.
* **Review findings** — merged after the rule findings and tagged
  ``source: "ai"``. They are advisory: severity is capped at ``medium`` unless
  ``AI_FINDINGS_CAN_BLOCK`` is set, so a false positive cannot fail a PR.

Any model failure leaves the deterministic output untouched and is recorded in
``result["ai"]``. The API key is never logged.
"""
from __future__ import annotations

import sys
from typing import Any, Mapping

from ..ai import prompts
from ..ai.analyze import review_in_batches
from ..ai.client import complete_json, metadata_model, review_model
from ..ai.schemas import validate_metadata
from ..ai.telemetry import Recorder
from ..common.errors import AIError

_OFF = {"0", "false", "no", "off"}
_ON = {"1", "true", "yes", "on"}

#: Metadata fields the model may replace. Everything else stays rule-derived.
PROSE_KEYS = ("title", "summary", "description", "review_focus", "rollback")

#: Highest severity an advisory model finding is reported at.
ADVISORY_CAP = "medium"
_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def enabled(env: Mapping[str, str] | None) -> bool:
    """True when a key is configured and ``AI_ENABLED`` does not switch it off."""
    if env is None:
        return False
    key = (env.get("NVIDIA_API_KEY") or env.get("AI_API_KEY") or "").strip()
    return bool(key) and (env.get("AI_ENABLED") or "").strip().lower() not in _OFF


def _findings_can_block(env: Mapping[str, str]) -> bool:
    return (env.get("AI_FINDINGS_CAN_BLOCK") or "").strip().lower() in _ON


def _fail(status: dict[str, Any], section: str, exc: Exception) -> None:
    # AIError messages never contain the key; other exceptions are reduced to
    # their type so nothing unexpected reaches the log.
    detail = str(exc) if isinstance(exc, AIError) else f"Unexpected {type(exc).__name__}."
    status[section] = {"status": "error", "error": detail}
    print(f"::warning::AI {section} failed; keeping rule output: {detail}", file=sys.stderr)


def overlay_metadata(
    metadata: dict[str, Any],
    context: Mapping[str, Any],
    diff: str,
    *,
    env: Mapping[str, str],
    status: dict[str, Any],
    client=None,
    recorder: Recorder | None = None,
) -> None:
    """Replace the rule-written prose in ``metadata`` with the model's, in place."""
    print("::group::AI section: metadata")
    try:
        data = complete_json(
            model=metadata_model(env),
            system=prompts.SYSTEM,
            user=prompts.metadata_prompt(
                context, diff[: prompts.DIFF_BUDGET], partial=len(diff) > prompts.DIFF_BUDGET
            ),
            validator=validate_metadata,
            client=client,
            env=env,
            recorder=recorder,
            section="metadata",
        )
    except Exception as exc:  # fail safe: the rule output stands
        _fail(status, "metadata", exc)
        return
    finally:
        print("::endgroup::")
    replaced = [key for key in PROSE_KEYS if data.get(key)]
    for key in replaced:
        metadata[key] = data[key]
    status["metadata"] = {"status": "ok", "replaced": replaced}


def add_review_findings(
    review: dict[str, Any],
    context: Mapping[str, Any],
    diff: str,
    *,
    security_only: bool,
    env: Mapping[str, str],
    status: dict[str, Any],
    client=None,
    recorder: Recorder | None = None,
) -> None:
    """Append model findings to the rule review, in place."""
    print("::group::AI section: review")
    try:
        data = review_in_batches(
            context, diff, security_only=security_only, client=client, env=env,
            recorder=recorder,
        )
    except Exception as exc:
        _fail(status, "review", exc)
        return
    finally:
        print("::endgroup::")

    can_block = _findings_can_block(env)
    seen = {
        (f.get("path"), f.get("line"), str(f.get("title", "")).lower())
        for f in review.get("findings") or []
    }
    added = 0
    for finding in data.get("findings") or []:
        key = (finding.get("path"), finding.get("line"), str(finding.get("title", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        item = dict(finding, source="ai")
        if not can_block and _RANK.get(item["severity"], 0) > _RANK[ADVISORY_CAP]:
            item["model_severity"] = item["severity"]
            item["severity"] = ADVISORY_CAP
        review.setdefault("findings", []).append(item)
        added += 1

    # Files the model could not cover are reported, never hidden; the rules still
    # saw them, so this is a note rather than an incomplete review.
    missed = data.get("unreviewed_paths") or []
    notes = [str(review.get("note") or "").strip(), f"Model review added {added} finding(s)."]
    if missed:
        notes.append(f"Model did not review {len(missed)} file(s).")
    review["note"] = " ".join(n for n in notes if n)
    status["review"] = {
        "status": "ok", "added": added, "batches": data.get("batches"),
        "succeeded_batches": data.get("succeeded_batches", data.get("batches")),
        "unreviewed_paths": missed,
    }
