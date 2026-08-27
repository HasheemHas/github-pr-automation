"""Push a previously validated patch to the target PR branch.

Run as ``python -m scripts.gitops.push``.

This is the only module that both holds a write token and touches the target's
git refs. It runs NO target-repository commands: no npm, no pip, no test suite,
no hooks. It re-verifies the patch digest recorded by the isolated validation
job, re-verifies the PR head SHA, then applies and pushes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ..common.config import artifact_dir
from ..common.errors import AutomationError, GitHubError
from ..github.api import GitHubClient
from .patches import check_paths, verify_changed_paths

SHA_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")
BOT_NAME = "pr-automation[bot]"
BOT_EMAIL = "pr-automation[bot]@users.noreply.github.com"


def _run(args: list[str], cwd: str, *, token: str | None = None) -> tuple[int, str]:
    """Run a git command. Hooks are disabled and the token never reaches argv."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if token:
        # Supply credentials via an askpass-free header so the token stays out of
        # both argv and the stored remote URL.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if token:
        output = output.replace(token, "***")
    return completed.returncode, output


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    cwd = env.get("TARGET_WORKDIR") or "target"

    try:
        token = env["TARGET_GITHUB_TOKEN"]
        full_repo = env["TARGET_FULL_REPO"]
        number = int(env["TARGET_PR_NUMBER"])
        head_ref = env["TARGET_HEAD_REF"]
        expected_head = env["TARGET_HEAD_SHA"]
        expected_base = env.get("TARGET_BASE_SHA") or ""
        push_mode = env.get("PUSH_MODE") or "fix"
        allowed = json.loads(env.get("PATCH_ALLOWED_PATHS") or "[]")
        forbidden = json.loads(env.get("PATCH_FORBIDDEN_PATHS") or "[]")
        message = env.get("COMMIT_MESSAGE") or "fix: apply validated automated patch"
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"::error::Missing or invalid push inputs: {exc}", file=sys.stderr)
        return 2

    validation = out_dir / "patch-validation.json"
    patch_file = out_dir / "validated-patch.diff"
    if not validation.is_file() or not patch_file.is_file():
        print("No validated patch artifact; nothing to push.")
        return 0
    record = json.loads(validation.read_text(encoding="utf-8"))
    if record.get("status") != "validated":
        print(f"Patch was not validated ({record.get('reason')}); nothing to push.")
        return 0

    patch_text = patch_file.read_text(encoding="utf-8")
    digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    if digest != record.get("sha256"):
        print(
            "::error::Validated patch digest mismatch; refusing to push.", file=sys.stderr
        )
        return 3

    try:
        declared = check_paths(patch_text, list(allowed), list(forbidden))
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 3

    # Re-verify the PR head immediately before writing.
    client = GitHubClient(token)
    try:
        pr = client.get(f"/repos/{full_repo}/pulls/{number}")
    except GitHubError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    current = ((pr or {}).get("head") or {}).get("sha") or ""
    current_base = ((pr or {}).get("base") or {}).get("sha") or ""
    if pr.get("state") != "open":
        print(f"::error::PR #{number} is no longer open; nothing was pushed.", file=sys.stderr)
        return 4
    if not SHA_PATTERN.match(expected_head) or current != expected_head:
        print(
            f"::error::PR #{number} head moved from {expected_head[:12]} to {current[:12]}; "
            "nothing was pushed. Re-run the workflow.",
            file=sys.stderr,
        )
        return 4

    if push_mode == "resolve-conflicts" and (
        not SHA_PATTERN.match(expected_base) or current_base != expected_base
    ):
        print(
            f"::error::PR #{number} base moved from {expected_base[:12]} to "
            f"{current_base[:12]}; nothing was pushed. Re-run the workflow.",
            file=sys.stderr,
        )
        return 4

    for args in (
        ["config", "user.name", BOT_NAME],
        ["config", "user.email", BOT_EMAIL],
        ["checkout", "--detach", expected_head],
    ):
        code, output = _run(args, cwd)
        if code != 0:
            print(f"::error::git {args[0]} failed: {output.strip()[:500]}", file=sys.stderr)
            return 1

    if push_mode == "resolve-conflicts":
        code, output = _run(["fetch", "--no-tags", "origin", expected_base], cwd, token=token)
        if code != 0:
            print(f"::error::Could not fetch the verified base: {output.strip()[:500]}",
                  file=sys.stderr)
            return 1
        code, output = _run(["merge", "--no-commit", "--no-ff", expected_base], cwd)
        if code == 0:
            print("::error::The verified revisions no longer produce a conflict.", file=sys.stderr)
            return 4
        code, unmerged = _run(["diff", "--name-only", "--diff-filter=U"], cwd)
        if code != 0 or not unmerged.strip():
            print(f"::error::Merge failed without resolvable file conflicts: {output.strip()[:500]}",
                  file=sys.stderr)
            return 1

    code, output = _run(["apply", "--whitespace=nowarn", str(patch_file)], cwd)
    if code != 0:
        print(f"::error::git apply failed: {output.strip()[:500]}", file=sys.stderr)
        return 1

    if push_mode == "resolve-conflicts":
        touched = declared
    else:
        try:
            touched = verify_changed_paths(cwd, allowed, forbidden)
        except AutomationError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 3
    if not touched:
        print("Patch applied but produced no changes; nothing to push.")
        return 0

    for args in (
        ["add", "--", *touched],
    ):
        code, output = _run(args, cwd)
        if code != 0:
            print(f"::error::git {args[0]} failed: {output.strip()[:500]}", file=sys.stderr)
            return 1

    if push_mode == "resolve-conflicts":
        code, unmerged = _run(["diff", "--name-only", "--diff-filter=U"], cwd)
        if code != 0 or unmerged.strip():
            print(f"::error::Resolution left unmerged paths: {unmerged.strip()[:500]}", file=sys.stderr)
            return 3

    code, output = _run(["commit", "--no-verify", "-m", message], cwd)
    if code != 0:
        print(f"::error::git commit failed: {output.strip()[:500]}", file=sys.stderr)
        return 1

    # Fast-forward-only push: if the branch advanced, the push is rejected.
    code, output = _run(
        [
            "push",
            f"https://github.com/{full_repo}.git",
            f"HEAD:refs/heads/{head_ref}",
            f"--force-with-lease=refs/heads/{head_ref}:{expected_head}",
        ],
        cwd,
        token=token,
    )
    if code != 0:
        print(f"::error::Push rejected: {output.strip()[:500]}", file=sys.stderr)
        return 1
    code, pushed_sha = _run(["rev-parse", "HEAD"], cwd)
    output_file = env.get("GITHUB_OUTPUT")
    if code == 0 and output_file:
        with Path(output_file).open("a", encoding="utf-8") as handle:
            handle.write(f"pushed_sha={pushed_sha.strip()}\n")
    print(f"Pushed {len(touched)} file(s) to {full_repo}:{head_ref}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
