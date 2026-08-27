"""Validate a candidate patch inside the credential-free job.

Run as ``python -m scripts.gitops.validate``.

Applies the patch to the checked-out target tree, re-checks every touched path
against the allowlist, then runs the configured quality commands. Emits
``patch-validation.json`` and, only on success, republishes the exact bytes that
were validated as ``validated-patch.diff``.

The privileged push job applies only that file, and verifies its digest first, so
the patch that gets pushed is byte-identical to the patch that was tested.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import subprocess
from pathlib import Path

from ..common.commands import run_commands, summarise
from ..common.config import artifact_dir, load_defaults, load_repositories, resolve_target
from ..common.errors import AutomationError
from ..quality.run import build_runner, collect, load_detection
from .patches import apply_patch, check_paths, verify_changed_paths


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    cwd = env.get("TARGET_WORKDIR") or "target"
    patch_name = env.get("PATCH_FILE") or "fix.patch"
    patch_file = out_dir / patch_name

    def fail(reason: str, code: int = 1) -> int:
        (out_dir / "patch-validation.json").write_text(
            json.dumps({"status": "rejected", "reason": reason}, indent=2) + "\n", encoding="utf-8"
        )
        print(f"::warning::Patch rejected: {reason}")
        return code

    if not patch_file.is_file() or not patch_file.read_text(encoding="utf-8").strip():
        return fail("No candidate patch was produced.", code=0)

    patch_text = patch_file.read_text(encoding="utf-8")
    try:
        allowed = json.loads(env.get("PATCH_ALLOWED_PATHS") or "[]")
        forbidden = json.loads(env.get("PATCH_FORBIDDEN_PATHS") or "[]")
        target = resolve_target(
            env["TARGET_REPOSITORY"],
            env={"TARGET_ORG": env.get("TARGET_ORG") or "validation-only"},
            repositories=load_repositories(env["CONFIG_FILE"]),
            defaults=load_defaults(env["CONFIG_FILE"]),
        )
        declared = check_paths(patch_text, allowed, forbidden)
    except (AutomationError, KeyError, json.JSONDecodeError) as exc:
        return fail(str(exc))

    ok, output = apply_patch(patch_file, cwd=cwd, check_only=True)
    if not ok:
        return fail(f"Patch does not apply cleanly: {output.strip()[:500]}")
    ok, output = apply_patch(patch_file, cwd=cwd)
    if not ok:
        return fail(f"Patch failed to apply: {output.strip()[:500]}")

    if patch_name == "resolution.patch":
        # The checkout deliberately contains a pending merge. Base-branch changes
        # outside the conflict allowlist are part of that merge, not model/script
        # edits, so validate only the declared resolution paths and ensure every
        # unmerged index entry is cleared.
        staged = subprocess.run(["git", "add", "--", *declared], cwd=cwd,
                                capture_output=True, text=True)
        if staged.returncode != 0:
            return fail(f"Could not stage resolved paths: {staged.stderr.strip()[:500]}")
        unresolved = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=cwd,
            capture_output=True, text=True,
        )
        if unresolved.returncode != 0 or unresolved.stdout.strip():
            return fail("The deterministic resolution did not resolve every conflict.")
        touched = declared
    else:
        try:
            touched = verify_changed_paths(cwd, allowed, forbidden)
        except AutomationError as exc:
            return fail(str(exc))

    # A patch is only pushed if it passed validation, so the validation must be
    # tamper-resistant: target commands run in a container that cannot see the
    # central scripts or this artifact directory. No sandbox, no push.
    try:
        runner, sandbox_info = build_runner(target, cwd, require_sandbox=True)
    except AutomationError as exc:
        return fail(str(exc))

    commands = collect(target, load_detection(env))
    results = run_commands(commands, cwd=cwd, runner=runner) if commands else []
    quality = summarise(results)
    quality["sandbox"] = sandbox_info
    if commands and quality["status"] != "pass":
        (out_dir / "quality-result.json").write_text(
            json.dumps(quality, indent=2) + "\n", encoding="utf-8"
        )
        return fail("Quality commands failed with the patch applied; nothing will be pushed.")

    digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    (out_dir / "validated-patch.diff").write_text(patch_text, encoding="utf-8")
    (out_dir / "patch-validation.json").write_text(
        json.dumps(
            {
                "status": "validated",
                "sha256": digest,
                "declared_paths": declared,
                "changed_paths": touched,
                "quality": quality,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Patch validated ({len(touched)} file(s)); sha256={digest[:16]}...")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
