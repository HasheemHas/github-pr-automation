"""Propose the next release version for a target repository. Read-only.

Run as ``python -m scripts.release.propose``.

Lists every tag (paginated), sorts numerically, and prints the next version. It
creates no tag and no release: release creation is a Phase 2 decision that needs
an explicit Contents-write approval per target repository.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ..common.errors import AutomationError
from ..github.api import GitHubClient
from .version import format_version, latest_version, next_version


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        full_repo = env["TARGET_FULL_REPO"]
        token = env["TARGET_GITHUB_TOKEN"]
    except KeyError as exc:
        print(f"::error::Missing required environment variable: {exc}", file=sys.stderr)
        return 2

    client = GitHubClient(token)
    try:
        tags = client.paginate(f"/repos/{full_repo}/tags", label="tags")
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    names = [t.get("name", "") for t in tags if isinstance(t, dict)]

    labels = [x for x in (env.get("RELEASE_LABELS") or "").split(",") if x.strip()]
    try:
        proposed = next_version(names, labels, env.get("EXPLICIT_VERSION") or "")
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    current = format_version(latest_version(names))
    print(f"Tags found: {len(names)}")
    print(f"Latest semantic version: v{current}")
    print(f"Proposed next version: v{proposed}")
    print("::notice::No tag or release was created. Release creation is not automated yet.")

    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(
                f"## Release proposal\n\n- Repository: `{full_repo}`\n"
                f"- Latest tag: `v{current}`\n- Proposed: `v{proposed}`\n"
                "- Nothing was created.\n"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
