"""Unified-diff inspection and safe application.

A patch is only ever applied after every path it touches has been checked against
the allowlist and the global deny list. Path parsing is done here rather than
trusting the model's own ``files`` list.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from ..common.errors import AutomationError
from ..common.globs import matches

_DIFF_GIT = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_MINUS = re.compile(r"^--- (?:a/)?(.+?)\s*$", re.MULTILINE)
_PLUS = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$", re.MULTILINE)


def patch_paths(patch: str) -> list[str]:
    """Every path a unified diff touches, de-duplicated and sorted."""
    paths: set[str] = set()
    for old, new in _DIFF_GIT.findall(patch or ""):
        paths.update({old.strip(), new.strip()})
    for match in _MINUS.finditer(patch or ""):
        paths.add(match.group(1).strip())
    for match in _PLUS.finditer(patch or ""):
        paths.add(match.group(1).strip())
    paths.discard("/dev/null")
    return sorted(p for p in paths if p and not p.startswith("/") and ".." not in Path(p).parts)


def check_paths(patch: str, allowed: Sequence[str], forbidden: Sequence[str]) -> list[str]:
    """Validate every touched path. Returns the paths; raises on any violation."""
    paths = patch_paths(patch)
    if not paths:
        raise AutomationError("The patch touches no recognisable file paths.")
    for path in paths:
        if any(matches(pattern, path) for pattern in forbidden):
            raise AutomationError(f"Patch touches a forbidden path: {path}")
        if not any(matches(pattern, path) for pattern in allowed):
            raise AutomationError(f"Patch touches a path outside the allowlist: {path}")
    # A patch that tries to escape the tree or write absolute paths is rejected above.
    return paths


def apply_patch(patch_file: str | Path, *, cwd: str | Path, check_only: bool = False,
                runner=subprocess.run) -> tuple[bool, str]:
    """Apply (or dry-run) a patch with ``git apply``. Never uses ``-3`` or ``--reject``."""
    args = ["git", "apply", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    args.append(str(patch_file))
    completed = runner(args, cwd=str(cwd), capture_output=True, text=True)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output


def changed_files(cwd: str | Path, runner=subprocess.run) -> list[str]:
    """Paths git reports as modified in the working tree."""
    completed = runner(
        ["git", "status", "--porcelain=1", "-z"], cwd=str(cwd), capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise AutomationError("Could not read git status in the target working tree.")
    entries = [e for e in (completed.stdout or "").split("\0") if e.strip()]
    return sorted({entry[3:] for entry in entries if len(entry) > 3})


def verify_changed_paths(cwd: str | Path, allowed: Iterable[str], forbidden: Iterable[str],
                         runner=subprocess.run) -> list[str]:
    """Confirm the applied patch changed only allowlisted paths."""
    allowed_list, forbidden_list = list(allowed), list(forbidden)
    files = changed_files(cwd, runner=runner)
    for path in files:
        if any(matches(pattern, path) for pattern in forbidden_list):
            raise AutomationError(f"Applied patch modified a forbidden path: {path}")
        if not any(matches(pattern, path) for pattern in allowed_list):
            raise AutomationError(f"Applied patch modified a path outside the allowlist: {path}")
    return files
