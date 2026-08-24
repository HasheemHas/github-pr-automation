"""AI merge-conflict resolution. Fail-safe: no confident patch means no patch.

Run as ``python -m scripts.ai.resolve``.

Consumes ``conflicts.json`` produced by the isolated merge-probe job and writes
``resolution.json`` plus, only when the model is confident, ``resolution.patch``.
The patch is validated in a credential-free job before anything is pushed.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping

from ..common.config import artifact_dir
from ..common.errors import AIError
from ..common.globs import matches
from . import prompts
from .client import complete_json, review_model
from .schemas import validate_patch


def resolve(
    context: Mapping[str, Any],
    conflicts: list[Mapping[str, Any]],
    allowed_paths: list[str],
    *,
    client=None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Ask for a resolution patch, refusing anything outside the allowlist."""
    if not conflicts:
        return {"status": "skipped", "reason": "No merge conflicts were detected.", "data": None}
    if not allowed_paths:
        return {
            "status": "skipped",
            "reason": "No resolve_allowed_paths are configured for this repository.",
            "data": None,
        }

    outside = [
        c.get("path", "")
        for c in conflicts
        if not any(matches(p, c.get("path", "")) for p in allowed_paths)
    ]
    if outside:
        return {
            "status": "refused",
            "reason": "Conflicts touch paths outside the allowlist: " + ", ".join(sorted(outside)),
            "data": None,
        }

    try:
        data = complete_json(
            model=review_model(env),
            system=prompts.SYSTEM_PATCH,
            user=prompts.conflict_prompt(context, list(conflicts), allowed_paths),
            validator=validate_patch,
            client=client,
            env=env,
            max_tokens=8192,
        )
    except AIError as exc:
        print(f"::warning::Conflict resolution failed: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc), "data": None}

    if not data["patch"].strip() or not data["confident"]:
        return {
            "status": "skipped",
            "reason": "The model was not confident; no resolution will be applied.",
            "data": data,
        }
    bad = [f for f in data["files"] if not any(matches(p, f) for p in allowed_paths)]
    if bad:
        return {
            "status": "refused",
            "reason": "Proposed resolution touches disallowed paths: " + ", ".join(sorted(bad)),
            "data": None,
        }
    return {"status": "ok", "data": data}


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    try:
        context = json.loads((out_dir / "pr-context.json").read_text(encoding="utf-8"))
        conflicts_file = out_dir / "conflicts.json"
        payload = json.loads(conflicts_file.read_text(encoding="utf-8")) if conflicts_file.exists() else {}
        conflicts = payload.get("conflicts") or []
        allowed = json.loads(env.get("RESOLVE_ALLOWED_PATHS") or "[]")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not load conflict inputs: {exc}", file=sys.stderr)
        return 1

    result = resolve(context, conflicts, list(allowed), env=env)
    (out_dir / "resolution.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if result["status"] == "ok":
        (out_dir / "resolution.patch").write_text(result["data"]["patch"], encoding="utf-8")
        print("Candidate resolution patch written for isolated validation.")
    else:
        print(f"No resolution patch: {result.get('reason') or result.get('error')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
