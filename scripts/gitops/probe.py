"""Probe a target PR for merge conflicts, without executing target code.

Run as ``python -m scripts.gitops.probe``.

Runs in the source-preparation job. It performs a local ``git merge --no-commit``
against the base ref and records the conflicted hunks. git operations do not run
repository-authored code (no hooks are installed on a fresh clone).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..common.config import artifact_dir

MAX_CONFLICT_BYTES = 20000
MAX_CONFLICT_FILES = 20


def _git(args: list[str], cwd: str, runner=subprocess.run) -> tuple[int, str]:
    completed = runner(["git", *args], cwd=cwd, capture_output=True, text=True)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _stage(cwd: str, stage: int, path: str, runner=subprocess.run) -> str | None:
    """Read one merge-index stage without interpreting repository content."""
    code, content = _git(["show", f":{stage}:{path}"], cwd, runner)
    return content if code == 0 else None


def probe(cwd: str, base_ref: str, runner=subprocess.run) -> dict:
    """Attempt the merge and collect conflicted files."""
    _git(["config", "user.name", "pr-automation"], cwd, runner)
    _git(["config", "user.email", "pr-automation@users.noreply.github.com"], cwd, runner)
    code, fetch_output = _git(["fetch", "--no-tags", "origin", base_ref], cwd, runner)
    if code != 0:
        return {"status": "error", "reason": f"Could not fetch base ref {base_ref}.",
                "detail": fetch_output[-2000:], "conflicts": []}

    code, merge_output = _git(["merge", "--no-commit", "--no-ff", "FETCH_HEAD"], cwd, runner)
    if code == 0:
        _git(["merge", "--abort"], cwd, runner)
        return {"status": "clean", "reason": "The branch merges cleanly.", "conflicts": []}

    code, listing = _git(["diff", "--name-only", "--diff-filter=U"], cwd, runner)
    paths = [line.strip() for line in listing.splitlines() if line.strip()]
    conflicts = []
    for path in paths[:MAX_CONFLICT_FILES]:
        try:
            content = (Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        base = _stage(cwd, 1, path, runner)
        ours = _stage(cwd, 2, path, runner)
        theirs = _stage(cwd, 3, path, runner)
        conflicts.append({
            "path": path,
            "conflicted_content": content[:MAX_CONFLICT_BYTES],
            "base": base[:MAX_CONFLICT_BYTES] if base is not None else None,
            "ours": ours[:MAX_CONFLICT_BYTES] if ours is not None else None,
            "theirs": theirs[:MAX_CONFLICT_BYTES] if theirs is not None else None,
            "content_truncated": any(
                value is not None and len(value) > MAX_CONFLICT_BYTES
                for value in (content, base, ours, theirs)
            ),
        })

    return {
        "status": "conflicted",
        "reason": merge_output[-2000:],
        "conflict_count": len(paths),
        "truncated": len(paths) > MAX_CONFLICT_FILES,
        "head_sha": _git(["rev-parse", "HEAD"], cwd, runner)[1].strip(),
        "base_sha": _git(["rev-parse", "FETCH_HEAD"], cwd, runner)[1].strip(),
        "conflicts": conflicts,
    }


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    cwd = env.get("TARGET_WORKDIR") or "target"
    base_ref = env.get("TARGET_BASE_SHA") or env.get("TARGET_BASE_REF") or "main"
    out_dir = artifact_dir(env)

    if not Path(cwd, ".git").is_dir():
        print(f"::error::{cwd} is not a git working tree.", file=sys.stderr)
        return 2

    result = probe(cwd, base_ref)
    (out_dir / "conflicts.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Merge probe: {result['status']} ({len(result['conflicts'])} conflicted file(s))")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
