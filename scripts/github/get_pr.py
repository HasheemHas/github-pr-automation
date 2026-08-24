"""Fetch and validate target PR context with a GitHub App installation token.

Run as ``python -m scripts.github.get_pr``.

Writes into the artifact directory:
  pr-context.json     sanitised PR metadata for the AI and report jobs
  changed-files.json  the full changed-path list
  pr.diff             the unified diff

Nothing in this module executes target repository code.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from ..common.config import artifact_dir
from ..common.errors import AutomationError, GitHubError, LimitExceeded
from ..common.globs import matching_paths
from .api import GitHubClient

SHA_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")
FULL_REPO_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+\Z")

DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_COMMITS = 500
DEFAULT_MAX_COMMENTS = 500
DEFAULT_MAX_DIFF_BYTES = 2_000_000
DEFAULT_MAX_TEST_FILES = 500

#: Test-file patterns used to inventory the whole target tree. Test-gap analysis
#: needs every existing test, not only the ones this PR happens to touch.
TEST_PATTERNS = (
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test/**",
    "__tests__/**",
    "**/__tests__/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.js",
    "**/*.spec.jsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*_spec.rb",
    "**/conftest.py",
)


def _limit(env: Mapping[str, str], name: str, default: int) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if raw.lower() in {"none", "0", "unlimited"}:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise AutomationError(f"{name} must be an integer, 'none' or empty.") from exc


def build_context(client: GitHubClient, full_repo: str, number: int,
                  env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Fetch the PR and every paginated collection the pipeline needs."""
    environ = os.environ if env is None else env
    if not FULL_REPO_PATTERN.match(full_repo):
        raise AutomationError(f"Invalid target repository {full_repo!r}.")
    if number < 1:
        raise AutomationError("Target PR number must be a positive integer.")

    pr = client.get(f"/repos/{full_repo}/pulls/{number}")
    if not isinstance(pr, dict):
        raise GitHubError("Unexpected pull request payload.")
    if pr.get("state") != "open":
        raise AutomationError(f"Target PR #{number} is not open.")
    head_sha = ((pr.get("head") or {}).get("sha") or "")
    if not SHA_PATTERN.match(head_sha):
        raise AutomationError(f"Target PR #{number} has no valid head SHA.")

    base = f"/repos/{full_repo}/pulls/{number}"
    files = client.paginate(
        f"{base}/files", limit=_limit(environ, "MAX_PR_FILES", DEFAULT_MAX_FILES), label="changed files"
    )
    commits = client.paginate(
        f"{base}/commits", limit=_limit(environ, "MAX_PR_COMMITS", DEFAULT_MAX_COMMITS), label="commits"
    )
    review_comments = client.paginate(
        f"{base}/comments",
        limit=_limit(environ, "MAX_PR_COMMENTS", DEFAULT_MAX_COMMENTS),
        label="review comments",
    )
    issue_comments = client.paginate(
        f"/repos/{full_repo}/issues/{number}/comments",
        limit=_limit(environ, "MAX_PR_COMMENTS", DEFAULT_MAX_COMMENTS),
        label="issue comments",
    )
    labels = client.paginate(f"/repos/{full_repo}/issues/{number}/labels", label="labels")

    head_repo = ((pr.get("head") or {}).get("repo") or {})
    return {
        "repository": full_repo,
        "number": number,
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "author": ((pr.get("user") or {}).get("login") or ""),
        "draft": bool(pr.get("draft")),
        "head_sha": head_sha,
        "head_ref": ((pr.get("head") or {}).get("ref") or ""),
        "head_repo": head_repo.get("full_name") or "",
        "from_fork": (head_repo.get("full_name") or "") != full_repo,
        "base_sha": ((pr.get("base") or {}).get("sha") or ""),
        "base_ref": ((pr.get("base") or {}).get("ref") or ""),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "files": [f.get("filename", "") for f in files if isinstance(f, dict)],
        "file_stats": [
            {
                "path": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
            }
            for f in files
            if isinstance(f, dict)
        ],
        "commits": [
            ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0]
            for c in commits
            if isinstance(c, dict)
        ],
        "review_comment_count": len(review_comments),
        "issue_comment_count": len(issue_comments),
        "existing_reviewers": sorted(
            {
                (r.get("login") or "")
                for r in (pr.get("requested_reviewers") or [])
                if isinstance(r, dict) and r.get("login")
            }
        ),
        "existing_reviewer_teams": sorted(
            {
                (t.get("slug") or "")
                for t in (pr.get("requested_teams") or [])
                if isinstance(t, dict) and t.get("slug")
            }
        ),
        "labels": sorted({(x.get("name") or "") for x in labels if isinstance(x, dict)}),
    }


def fetch_diff(client: GitHubClient, full_repo: str, number: int, max_bytes: int) -> str:
    """Fetch the PR diff, truncating only with an explicit marker."""
    diff = client.get_text(
        f"/repos/{full_repo}/pulls/{number}", accept="application/vnd.github.v3.diff"
    )
    if len(diff) > max_bytes:
        return diff[:max_bytes] + "\n[diff truncated by MAX_DIFF_BYTES]\n"
    return diff


def fetch_test_inventory(
    client: GitHubClient, full_repo: str, ref: str, limit: int = DEFAULT_MAX_TEST_FILES
) -> dict[str, Any]:
    """Inventory every test file in the target tree at ``ref``.

    One recursive tree read, no checkout and no code execution. Test-gap analysis
    was previously filtering the PR's own changed files, so a PR that touched no
    test file was told the repository had no tests at all.
    """
    try:
        tree = client.get(f"/repos/{full_repo}/git/trees/{ref}?recursive=1")
    except GitHubError as exc:
        return {"tests": [], "total_files": 0, "truncated": False,
                "error": f"Could not read the repository tree (HTTP {exc.status})."}

    entries = [
        entry.get("path", "")
        for entry in (tree or {}).get("tree") or []
        if isinstance(entry, dict) and entry.get("type") == "blob"
    ]
    tests = matching_paths(TEST_PATTERNS, entries)
    truncated_tree = bool((tree or {}).get("truncated"))
    return {
        "tests": sorted(tests)[:limit],
        "total_files": len(entries),
        "test_count": len(tests),
        # Either the tree API truncated, or we capped the list we hand the model.
        "truncated": truncated_tree or len(tests) > limit,
        "tree_truncated": truncated_tree,
    }


def fetch_codeowners(client: GitHubClient, full_repo: str, ref: str) -> str | None:
    """Return CODEOWNERS contents from the target repo, or None when absent."""
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        try:
            return client.get_text(
                f"/repos/{full_repo}/contents/{path}?ref={ref}",
                accept="application/vnd.github.raw",
            )
        except GitHubError as exc:
            if exc.status in (403, 404):
                continue
            raise
    return None


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        full_repo = env["TARGET_FULL_REPO"]
        number = int(env["TARGET_PR_NUMBER"])
        token = env["TARGET_GITHUB_TOKEN"]
    except (KeyError, ValueError) as exc:
        print(f"::error::Missing or invalid required environment: {exc}", file=sys.stderr)
        return 2

    out_dir = artifact_dir(env)
    client = GitHubClient(token)
    try:
        context = build_context(client, full_repo, number, env)
        max_diff = _limit(env, "MAX_DIFF_BYTES", DEFAULT_MAX_DIFF_BYTES) or DEFAULT_MAX_DIFF_BYTES
        diff = fetch_diff(client, full_repo, number, max_diff)
        codeowners = fetch_codeowners(client, full_repo, context["base_sha"])
        inventory = fetch_test_inventory(
            client,
            full_repo,
            context["head_sha"],
            _limit(env, "MAX_TEST_FILES", DEFAULT_MAX_TEST_FILES) or DEFAULT_MAX_TEST_FILES,
        )
    except LimitExceeded as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 3
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    context["has_codeowners"] = codeowners is not None
    context["repository_tests"] = inventory["tests"]
    context["test_inventory"] = {k: v for k, v in inventory.items() if k != "tests"}
    (out_dir / "pr-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    (out_dir / "changed-files.json").write_text(
        json.dumps({"files": context["files"], "count": len(context["files"])}, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "pr.diff").write_text(diff, encoding="utf-8")
    if codeowners is not None:
        (out_dir / "codeowners.txt").write_text(codeowners, encoding="utf-8")

    step_output = env.get("GITHUB_OUTPUT")
    if step_output:
        with Path(step_output).open("a", encoding="utf-8") as handle:
            handle.write(f"head_sha={context['head_sha']}\n")
            handle.write(f"head_ref={context['head_ref']}\n")
            handle.write(f"base_ref={context['base_ref']}\n")
            handle.write(f"from_fork={'true' if context['from_fork'] else 'false'}\n")
            handle.write(f"file_count={len(context['files'])}\n")
    print(f"Resolved {full_repo}#{number} at {context['head_sha']} "
          f"({len(context['files'])} changed files, "
          f"{len(context['repository_tests'])} test file(s) in the repository).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
