"""Run the AI portions of the pipeline. Fail-safe by construction.

Run as ``python -m scripts.ai.analyze``.

Each section is independent: a provider error, a malformed response or a schema
violation records ``status: "error"`` for that section only and never produces
placeholder content. The report job writes nothing for a section that did not
succeed, so an AI failure can never become a successful write.

This module never checks out or executes target repository code.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from ..common.config import artifact_dir
from ..common.errors import AIError
from . import prompts
from .chunking import (
    DEFAULT_BATCH_CHARS,
    DEFAULT_BATCH_FILES,
    DEFAULT_MAX_BATCHES,
    merge_findings,
    plan_batches,
)
from .client import complete_json, metadata_model, review_model
from .schemas import validate_metadata, validate_patch, validate_review, validate_tests


def _section(name: str, runner: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one AI section, converting any failure into a recorded non-result."""
    try:
        return {"status": "ok", "data": runner()}
    except AIError as exc:
        print(f"::warning::AI section '{name}' failed: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc), "data": None}
    except Exception as exc:  # never let an unexpected error look like success
        print(f"::warning::AI section '{name}' failed: {type(exc).__name__}.", file=sys.stderr)
        return {"status": "error", "error": f"Unexpected {type(exc).__name__}.", "data": None}


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def review_in_batches(
    context: Mapping[str, Any],
    diff: str,
    *,
    security_only: bool,
    client=None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Review the whole diff across as many requests as it takes.

    A single 40k-character prompt silently reviewed only the first few files of a
    large PR. This splits by file, then by hunk, and merges the findings. Anything
    beyond the batch cap is reported, never dropped quietly.
    """
    environ = env or {}
    batching = plan_batches(
        diff,
        batch_chars=_int_env(environ, "AI_REVIEW_BATCH_CHARS", DEFAULT_BATCH_CHARS),
        batch_files=_int_env(environ, "AI_REVIEW_BATCH_FILES", DEFAULT_BATCH_FILES),
        max_batches=_int_env(environ, "AI_REVIEW_MAX_BATCHES", DEFAULT_MAX_BATCHES),
    )
    changed = {p for p in (context.get("files") or []) if isinstance(p, str)}
    total_batches = len(batching.batches)

    if not total_batches:
        return {
            "findings": [],
            "dropped": 0,
            "batches": 0,
            "complete": True,
            "reviewed_paths": [],
            "unreviewed_paths": [],
            "note": "The diff was empty.",
        }

    groups: list[list[dict]] = []
    failures: list[str] = []
    dropped = 0
    for index, batch in enumerate(batching.batches, start=1):
        try:
            data = complete_json(
                model=review_model(env),
                system=prompts.SYSTEM,
                user=prompts.review_prompt(
                    context,
                    batch.text,
                    security_only=security_only,
                    batch=index,
                    batches=total_batches,
                    paths=batch.paths,
                ),
                validator=lambda payload: validate_review(payload, changed_paths=changed),
                client=client,
                env=env,
            )
        except AIError as exc:
            # One failed batch must not discard the batches that succeeded, and
            # must not be reported as a clean review either.
            failures.append(f"batch {index}/{total_batches} ({', '.join(batch.paths)}): {exc}")
            continue
        groups.append(data["findings"])
        dropped += data["dropped"]

    if failures and not groups:
        raise AIError("Every review batch failed: " + failures[0])

    failed_paths = sorted(
        {
            path
            for index, batch in enumerate(batching.batches, start=1)
            for path in batch.paths
            if any(f.startswith(f"batch {index}/") for f in failures)
        }
    )
    unreviewed = sorted(set(batching.skipped_paths) | set(failed_paths))
    notes = [n for n in (batching.skipped_reason,) if n]
    if failures:
        notes.append(f"{len(failures)} of {total_batches} review batch(es) failed.")

    return {
        "findings": merge_findings(groups),
        "dropped": dropped,
        "batches": total_batches,
        "succeeded_batches": len(groups),
        "complete": not unreviewed,
        "reviewed_paths": [p for p in batching.covered_paths if p not in failed_paths],
        "unreviewed_paths": unreviewed,
        "note": " ".join(notes),
        "failures": failures,
    }


def load_plan(env: Mapping[str, str]) -> dict[str, Any]:
    raw = env.get("PR_AUTOMATION_PLAN") or "{}"
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIError(f"PR_AUTOMATION_PLAN is not valid JSON: {exc.msg}.") from None
    if not isinstance(plan, dict):
        raise AIError("PR_AUTOMATION_PLAN must be a JSON object.")
    return plan


def analyse(
    plan: Mapping[str, Any],
    context: Mapping[str, Any],
    diff: str,
    *,
    client=None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Produce the analysis payload for the requested plan."""
    changed = {p for p in (context.get("files") or []) if isinstance(p, str)}
    result: dict[str, Any] = {
        "action": plan.get("action"),
        "head_sha": context.get("head_sha"),
        "models": {"metadata": metadata_model(env), "review": review_model(env)},
    }

    if plan.get("metadata"):
        partial = len(diff) > prompts.DIFF_BUDGET
        result["metadata"] = _section(
            "metadata",
            lambda: complete_json(
                model=metadata_model(env),
                system=prompts.SYSTEM,
                user=prompts.metadata_prompt(context, diff[: prompts.DIFF_BUDGET], partial=partial),
                validator=validate_metadata,
                client=client,
                env=env,
            ),
        )

    # ``security`` is the same review call narrowed to security categories.
    if plan.get("review") or plan.get("security"):
        security_only = bool(plan.get("security")) and not plan.get("review")
        result["review"] = _section(
            "security" if security_only else "review",
            lambda: review_in_batches(
                context, diff, security_only=security_only, client=client, env=env
            ),
        )
        result["review_scope"] = "security" if security_only else "full"

    if plan.get("tests"):
        # The whole-repository inventory, not just the tests this PR touched.
        existing = [p for p in (context.get("repository_tests") or []) if isinstance(p, str)]
        inventory = context.get("test_inventory") or {}
        result["tests"] = _section(
            "tests",
            lambda: complete_json(
                model=review_model(env),
                system=prompts.SYSTEM,
                user=prompts.tests_prompt(context, diff, existing, inventory=inventory),
                validator=validate_tests,
                client=client,
                env=env,
            ),
        )
        result["test_inventory"] = {
            "count": len(existing),
            "truncated": bool(inventory.get("truncated")),
            "source": "repository tree at head",
        }

    if plan.get("fix"):
        review = (result.get("review") or {}).get("data") or {}
        findings = [
            f for f in (review.get("findings") or [])
            if f.get("severity") in ("critical", "high", "medium")
        ]
        allowed = list(plan.get("fix_allowed_paths") or [])
        if not findings:
            result["fix"] = {
                "status": "skipped",
                "reason": "No actionable findings to fix.",
                "data": None,
            }
        elif not allowed:
            result["fix"] = {
                "status": "skipped",
                "reason": "No fix_allowed_paths are configured for this repository.",
                "data": None,
            }
        else:
            result["fix"] = _section(
                "fix",
                lambda: complete_json(
                    model=review_model(env),
                    system=prompts.SYSTEM_PATCH,
                    user=prompts.fix_prompt(context, diff, findings, allowed),
                    validator=validate_patch,
                    client=client,
                    env=env,
                ),
            )

    return result


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    try:
        plan = load_plan(env)
        context = json.loads((out_dir / "pr-context.json").read_text(encoding="utf-8"))
        diff_path = out_dir / "pr.diff"
        diff = diff_path.read_text(encoding="utf-8", errors="replace") if diff_path.exists() else ""
    except (AIError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not load AI inputs: {exc}", file=sys.stderr)
        return 1

    result = analyse(plan, context, diff, env=env)
    (out_dir / "analysis.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    # A patch is written as its own artifact only when it is non-empty.
    fix = (result.get("fix") or {}).get("data") or {}
    patch = fix.get("patch") or ""
    if patch.strip() and fix.get("confident"):
        (out_dir / "fix.patch").write_text(patch, encoding="utf-8")
        print("Candidate fix patch written for isolated validation.")
    elif (result.get("fix") or {}).get("status") == "ok":
        print("AI returned no confident patch; nothing will be pushed.")

    sections = {k: v.get("status") for k, v in result.items() if isinstance(v, dict) and "status" in v}
    print("AI sections: " + (json.dumps(sections) if sections else "none requested"))
    # Section failures are reported, not fatal: the report job renders them as
    # "NOT RUN" and leaves the target PR untouched for those sections.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
