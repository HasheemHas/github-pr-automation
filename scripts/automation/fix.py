"""Generate conservative, reproducible fixes against a checked-out PR head."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ..common.config import artifact_dir
from ..common.globs import matches
from .analyze import added_lines


def apply_safe_fixes(workdir: str | Path, diff: str, allowed: list[str]) -> list[str]:
    """Apply allowlisted, semantics-preserving rules only to PR-added lines."""
    root = Path(workdir).resolve()
    grouped: dict[str, list] = {}
    for item in added_lines(diff):
        is_bare_except = re.match(r"^(\s*)except\s*:(\s*(?:#.*)?)$", item.text)
        if item.text.rstrip(" \t") == item.text and not is_bare_except:
            continue
        if not any(matches(pattern, item.path) for pattern in allowed):
            continue
        grouped.setdefault(item.path, []).append(item)

    changed: list[str] = []
    for relative, items in grouped.items():
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        touched = False
        for item in items:
            index = item.line - 1
            if not 0 <= index < len(lines):
                continue
            ending = "\n" if lines[index].endswith("\n") else ""
            content = lines[index][:-1] if ending else lines[index]
            if content.endswith("\r"):
                content, ending = content[:-1], "\r" + ending
            # Refuse to edit if the checkout no longer matches the analysed diff.
            if content != item.text:
                continue
            replacement = content.rstrip(" \t")
            bare = re.match(r"^(\s*)except\s*:(\s*(?:#.*)?)$", replacement)
            if bare:
                replacement = f"{bare.group(1)}except Exception:{bare.group(2)}"
            replacement += ending
            if replacement != lines[index]:
                lines[index] = replacement
                touched = True
        if touched:
            path.write_text("".join(lines), encoding="utf-8")
            changed.append(relative)
    return sorted(changed)


def build_patch(workdir: str | Path, paths: list[str]) -> str:
    if not paths:
        return ""
    completed = subprocess.run(
        ["git", "diff", "--binary", "--", *paths], cwd=str(workdir),
        capture_output=True, text=True, errors="replace",
    )
    return completed.stdout if completed.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    workdir = env.get("TARGET_WORKDIR") or "target"
    try:
        allowed = json.loads(env.get("PATCH_ALLOWED_PATHS") or "[]")
        diff = (out_dir / "pr.diff").read_text(encoding="utf-8", errors="replace")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not load deterministic fix inputs: {exc}", file=sys.stderr)
        return 1
    changed = apply_safe_fixes(workdir, diff, list(allowed))
    patch = build_patch(workdir, changed)
    # Restore the pristine checkout: validation must apply and verify these bytes.
    if changed:
        subprocess.run(["git", "checkout", "--", *changed], cwd=workdir, check=False,
                       capture_output=True)
    patch_file = out_dir / "fix.patch"
    if patch.strip():
        patch_file.write_text(patch, encoding="utf-8")
        print(f"Generated a deterministic whitespace patch for {len(changed)} file(s).")
    else:
        patch_file.unlink(missing_ok=True)
        print("No safely auto-fixable changes were found.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
