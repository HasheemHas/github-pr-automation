"""Resolve only mechanically unambiguous three-way merge conflicts."""
from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from ..common.config import artifact_dir
from ..common.globs import matches

_MISSING = object()


def _merge_json(base: Any, ours: Any, theirs: Any) -> Any:
    """Three-way merge JSON values, rejecting a same-key semantic conflict."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if all(isinstance(value, dict) for value in (base, ours, theirs)):
        merged: dict[str, Any] = {}
        for key in sorted(set(base) | set(ours) | set(theirs)):
            b, o, t = base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING)
            value = _merge_json(b, o, t)
            if value is not _MISSING:
                merged[key] = value
        return merged
    raise ValueError("both sides changed the same JSON value")


def _patch(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))


def resolve(conflict_payload: Mapping[str, Any], allowed_paths: list[str]) -> dict[str, Any]:
    conflicts = conflict_payload.get("conflicts") or []
    if conflict_payload.get("status") == "clean":
        return {"status": "skipped", "reason": "The branch already merges cleanly.", "data": None}
    if conflict_payload.get("status") != "conflicted":
        return {"status": "error", "reason": str(conflict_payload.get("reason") or
                                                    "The merge probe failed."), "data": None}
    if conflict_payload.get("truncated"):
        return {"status": "refused", "reason": "Too many conflicted files for a complete resolution.",
                "data": None}
    if not conflicts:
        return {"status": "error", "reason": "Git reported a conflict but exposed no text hunks.",
                "data": None}

    patches: list[str] = []
    files: list[str] = []
    strategies: dict[str, str] = {}
    for item in conflicts:
        path = str(item.get("path") or "")
        if not path or not any(matches(pattern, path) for pattern in allowed_paths):
            return {"status": "refused", "reason": f"Conflict is outside resolve_allowed_paths: {path}",
                    "data": None}
        if item.get("content_truncated"):
            return {"status": "refused", "reason": f"Conflict content was truncated: {path}",
                    "data": None}
        base, ours, theirs = item.get("base"), item.get("ours"), item.get("theirs")
        before = item.get("conflicted_content")
        if not all(isinstance(value, str) for value in (base, ours, theirs, before)):
            return {"status": "refused", "reason": f"Binary or incomplete conflict: {path}",
                    "data": None}
        if ours == theirs:
            resolved, strategy = ours, "identical sides"
        elif ours == base:
            resolved, strategy = theirs, "only base branch changed"
        elif theirs == base:
            resolved, strategy = ours, "only PR branch changed"
        elif path.lower().endswith(".json"):
            try:
                resolved_value = _merge_json(json.loads(base), json.loads(ours), json.loads(theirs))
                resolved = json.dumps(resolved_value, indent=2, ensure_ascii=False) + "\n"
                strategy = "semantic JSON three-way merge"
            except (json.JSONDecodeError, ValueError):
                return {"status": "refused", "reason": (
                    f"Both sides changed the same JSON value in {path}; resolution requires review."
                ), "data": None}
        else:
            return {"status": "refused", "reason": (
                f"Both sides changed {path}; deterministic resolution would require guessing."
            ), "data": None}
        patch = _patch(path, before, resolved)
        if not patch:
            return {"status": "error", "reason": f"Could not represent the resolution for {path}.",
                    "data": None}
        patches.append(patch)
        files.append(path)
        strategies[path] = strategy
    return {"status": "ok", "data": {"patch": "".join(patches), "files": files,
            "strategies": strategies, "head_sha": conflict_payload.get("head_sha"),
            "base_sha": conflict_payload.get("base_sha")}}


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    try:
        payload = json.loads((out_dir / "conflicts.json").read_text(encoding="utf-8"))
        allowed = json.loads(env.get("RESOLVE_ALLOWED_PATHS") or "[]")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not load conflict inputs: {exc}", file=sys.stderr)
        return 1
    result = resolve(payload, list(allowed))
    (out_dir / "resolution.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    patch = ((result.get("data") or {}).get("patch") or "")
    if result.get("status") == "ok" and patch.strip():
        (out_dir / "resolution.patch").write_text(patch, encoding="utf-8")
        print("Generated a deterministic resolution for every conflicted file.")
    else:
        print(f"No automatic resolution: {result.get('reason')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
