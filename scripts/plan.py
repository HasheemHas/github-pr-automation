"""Validate the request and emit the execution plan.

Run as ``python -m scripts.plan``.

This runs before any credential is minted: an unknown action, an unlisted target
repository or a missing ``TARGET_ORG`` fails here, with no token in existence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .common import sandbox, summary
from .common.config import (
    DEFAULT_REPOSITORIES_CONFIG,
    artifact_dir,
    resolve_target,
)
from .common.dispatch import plan as build_plan
from .common.errors import AutomationError

TITLE_POLICIES = ("weak", "always", "never")
DEFAULT_TITLE_POLICY = "weak"

#: Order the steps are listed in the job summary.
STEP_ORDER = (
    "metadata",
    "labels",
    "reviewers",
    "review",
    "security",
    "quality",
    "tests",
    "fix",
    "resolve_conflicts",
)

#: Paths an AI-authored patch may never touch, whatever the target config says.
FORBIDDEN_FIX_PATHS = (
    ".github/**",
    "**/*.tf",
    "terraform/**",
    "helm/**",
    "**/Dockerfile",
    "docker-compose.*",
    "**/package-lock.json",
    "**/requirements*.txt",
    "**/pyproject.toml",
    "**/poetry.lock",
    "**/*.pem",
    "**/*.key",
    "**/CODEOWNERS",
)


def patch_allowlist(config: Mapping[str, Any], key: str) -> list[str]:
    """Read a patch path allowlist, dropping anything globally forbidden."""
    declared = config.get(key) or []
    if not isinstance(declared, list):
        raise AutomationError(f"{key} must be a list of glob patterns.")
    allowed: list[str] = []
    for pattern in declared:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        candidate = pattern.strip()
        if candidate in FORBIDDEN_FIX_PATHS:
            print(f"::warning::Ignoring globally forbidden patch path {candidate!r}.")
            continue
        allowed.append(candidate)
    return allowed


def write_outputs(values: Mapping[str, str], env: Mapping[str, str]) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    repository = env.get("TARGET_REPOSITORY", "")
    action = env.get("TARGET_ACTION", "")
    config_path = env.get("REPOSITORIES_CONFIG") or DEFAULT_REPOSITORIES_CONFIG

    try:
        execution = build_plan(action)
        target = resolve_target(repository, env=env, path=config_path)
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    execution["repository"] = target.repo
    execution["owner"] = target.owner
    execution["full_name"] = target.full_name
    execution["fix_allowed_paths"] = patch_allowlist(target.config, "fix_allowed_paths")
    execution["resolve_allowed_paths"] = patch_allowlist(target.config, "resolve_allowed_paths")
    execution["forbidden_patch_paths"] = list(FORBIDDEN_FIX_PATHS)

    metadata_config = target.config.get("metadata") or {}
    policy = str(metadata_config.get("title_policy") or DEFAULT_TITLE_POLICY).strip().lower()
    if policy not in TITLE_POLICIES:
        print(
            f"::warning::Unknown metadata.title_policy {policy!r}; "
            f"falling back to {DEFAULT_TITLE_POLICY!r}."
        )
        policy = DEFAULT_TITLE_POLICY
    execution["metadata_options"] = {"title_policy": policy}

    review_config = target.config.get("review") or {}
    execution["inline_comments"] = bool(review_config.get("inline_comments", True))

    # Patch pipelines require a working sandbox: target commands must not be able
    # to reach the validation artifacts that decide whether a patch is pushed.
    try:
        sandbox_config = sandbox.load(target.config)
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    execution["sandbox"] = {"enabled": sandbox_config.enabled, "image": sandbox_config.image}
    if execution["needs_privileged_write"] and not sandbox_config.usable:
        print(
            "::error::Action "
            f"{execution['action']!r} writes to the target repository, so target "
            "commands must run in a sandbox. Set sandbox.enabled and sandbox.image for "
            f"{target.repo!r} in config/repositories.yml, then re-run.",
            file=sys.stderr,
        )
        return 1

    out_dir = artifact_dir(env)
    (out_dir / "plan.json").write_text(json.dumps(execution, indent=2) + "\n", encoding="utf-8")

    write_outputs(
        {
            "owner": target.owner,
            "repo": target.repo,
            "full_name": target.full_name,
            "plan": json.dumps(execution, separators=(",", ":")),
            "needs_ai": str(execution["needs_ai"]).lower(),
            "needs_target_source": str(execution["needs_target_source"]).lower(),
            "needs_privileged_write": str(execution["needs_privileged_write"]).lower(),
            "needs_merge_probe": str(execution["needs_merge_probe"]).lower(),
            "run_metadata": str(execution["metadata"]).lower(),
            "run_labels": str(execution["labels"]).lower(),
            "run_reviewers": str(execution["reviewers"]).lower(),
            "run_review": str(execution["review"]).lower(),
            "run_security": str(execution["security"]).lower(),
            "run_quality": str(execution["quality"]).lower(),
            "run_tests": str(execution["tests"]).lower(),
            "run_fix": str(execution["fix"]).lower(),
            "run_resolve_conflicts": str(execution["resolve_conflicts"]).lower(),
            "sandbox_enabled": str(sandbox_config.usable).lower(),
            "sandbox_image": sandbox_config.image,
        },
        env,
    )
    print(f"Action '{execution['action']}' -> steps: {', '.join(execution['steps'])}")
    print(f"Target: {target.full_name}")

    summary.write(
        summary.heading("🧭 Plan"),
        summary.definitions(
            {
                "Target": f"`{target.full_name}` PR #{env.get('TARGET_PR_NUMBER', '?')}",
                "Action": f"`{execution['action']}`",
                "Steps": ", ".join(f"`{step}`" for step in execution["steps"]),
            }
        ),
        summary.table(
            ["Step", "Requested"],
            [
                [step.replace("_", "-"), summary.icon("PASS" if execution[step] else "NOT RUN")]
                for step in STEP_ORDER
            ],
        ),
        summary.definitions(
            {
                "AI required": "yes" if execution["needs_ai"] else "no",
                "Target checkout required": "yes" if execution["needs_target_source"] else "no",
                "Writes to git": "yes" if execution["needs_privileged_write"] else "no",
                "Sandbox": (
                    f"`{sandbox_config.image}`" if sandbox_config.usable
                    else "not configured (required for `fix` / `resolve-conflicts`)"
                ),
                "Title policy": f"`{policy}`",
            }
        ),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
