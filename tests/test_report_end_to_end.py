"""The report job end to end: artifacts in, PR comment and gate.json out."""
from __future__ import annotations

import json

import pytest

from scripts.common.dispatch import plan as build_plan
from scripts.github import report
from scripts.github.api import GitHubClient
from tests.conftest import FakeOpener

HEAD = "c" * 40

PR_CONTEXT = {
    "repository": "org/repo",
    "number": 7,
    "title": "feat: add invoice export",
    "body": "",
    "author": "author-login",
    "head_sha": HEAD,
    "files": ["backend/app/api.py", "frontend/src/App.tsx"],
    "file_stats": [
        {"path": "backend/app/api.py", "status": "modified", "additions": 20, "deletions": 3},
        {"path": "frontend/src/App.tsx", "status": "modified", "additions": 5, "deletions": 1},
    ],
    "commits": ["feat: add invoice export"],
    "additions": 25,
    "deletions": 4,
    "draft": False,
    "mergeable": True,
    "mergeable_state": "clean",
    "approvals": ["alice"],
    "changes_requested": [],
    "reviewed_by": ["alice"],
    "existing_reviewers": [],
    "existing_reviewer_teams": [],
    "labels": [],
    "repository_tests": ["tests/test_api.py"],
    "test_inventory": {"truncated": False},
}

ANALYSIS = {
    "metadata": {"status": "ok", "data": {
        "summary": "Updates the API.", "description": "", "change_type": "feature",
        "risk": "low", "components": ["backend"], "changes": [], "release_notes": [],
        "recommended_testing": [], "review_focus": [], "migration_notes": [],
        "rollback": "Revert.", "related_issues": [], "breaking_change": False,
        "breaking_change_details": "", "risk_reason": "small",
    }},
    "review": {"status": "ok", "data": {"findings": [], "complete": True}},
    "tests": {"status": "ok", "data": {"gaps": []}},
    "size": {"status": "ok", "data": {
        "status": "pass", "summary": "2 reviewable file(s), 29 changed line(s); risk low",
        "findings": [], "counts": {},
    }},
    "secrets": {"status": "ok", "data": {
        "status": "pass", "summary": "No credential-shaped values in 25 added line(s)",
        "findings": [], "counts": {}, "hits": [],
    }},
    "dependencies": {"status": "ok", "data": {
        "status": "pass", "summary": "No dependency manifests or lock files changed.",
        "findings": [], "counts": {},
    }},
    "migrations": {"status": "ok", "data": {
        "status": "pass", "summary": "No migration files changed.", "findings": [], "counts": {},
    }},
    "api": {"status": "ok", "data": {
        "status": "pass", "summary": "No interface changes detected", "findings": [],
        "counts": {}, "breaking_change": False,
    }},
    "release_notes": {"status": "ok", "data": {
        "status": "pass", "summary": "1 note(s)",
        "markdown": "### Added\n\n- Add invoice export\n", "sections": [],
        "operator_actions": [],
    }},
}

QUALITY = {
    "status": "pass",
    "results": [
        {"name": "Deps", "group": "install", "status": "pass"},
        {"name": "Lint", "group": "lint", "status": "pass"},
        {"name": "Tests", "group": "test", "status": "pass"},
        {"name": "Build", "group": "build", "status": "pass"},
    ],
}

SECURITY = {"status": "pass", "results": [{"name": "gitleaks", "status": "pass"}]}

COVERAGE = {
    "status": "pass",
    "reason": "88.0% of 25 changed line(s) covered.",
    "threshold": 80,
    "analysis": {
        "changed_line_coverage": 88.0, "measured_lines": 25, "covered_lines": 22,
        "uncovered_lines": 3, "denominator": 25,
        "files": [{"path": "backend/app/api.py", "report_path": "", "added_lines": 20,
                   "measured_lines": 18, "covered_lines": 15, "uncovered_lines": [31, 32, 33],
                   "uncovered_count": 3, "percent": 83.3, "not_executable": 2}],
        "unmeasured_files": [],
    },
}

CODEOWNERS = "/backend/ @backend-lead\n"

DIFF = """diff --git a/backend/app/api.py b/backend/app/api.py
--- a/backend/app/api.py
+++ b/backend/app/api.py
@@ -30,1 +30,4 @@
 existing
+added_one = 1
+added_two = 2
+added_three = 3
"""


def _routes():
    pr = {"title": "feat: add invoice export", "body": "", "state": "open",
          "head": {"sha": HEAD}}
    return {
        "GET /repos/org/repo/pulls/7": (pr, {}),
        "PATCH /repos/org/repo/pulls/7": (pr, {}),
        "GET /repos/org/repo/labels?per_page=100": ([], {}),
        "POST /repos/org/repo/labels": ({}, {}),
        "POST /repos/org/repo/issues/7/labels": ({}, {}),
        "GET /repos/org/repo/issues/7/comments?per_page=100": ([], {}),
        "POST /repos/org/repo/issues/7/comments": ({"id": 1}, {}),
        "GET /repos/org/repo/pulls/7/comments?per_page=100": ([], {}),
        "POST /repos/org/repo/pulls/7/reviews": ({}, {}),
        "POST /repos/org/repo/pulls/7/requested_reviewers": ({}, {}),
        "POST /repos/org/repo/check-runs": ({}, {}),
    }


@pytest.fixture()
def artifacts(tmp_path, monkeypatch):
    """An artifact directory populated the way the real jobs populate it."""
    directory = tmp_path / "pr-automation-artifacts"
    directory.mkdir()

    def write(name, payload):
        (directory / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    write("pr-context.json", PR_CONTEXT)
    write("analysis.json", ANALYSIS)
    write("quality-result.json", QUALITY)
    write("security-result.json", SECURITY)
    write("coverage-result.json", COVERAGE)
    (directory / "pr.diff").write_text(DIFF, encoding="utf-8")
    (directory / "codeowners.txt").write_text(CODEOWNERS, encoding="utf-8")

    plan = dict(build_plan("all"))
    plan.update({"owner": "org", "repo": "repo", "full_name": "org/repo",
                 "metadata_options": {"title_policy": "weak", "sections": ["description"]},
                 "reviewer_options": {"codeowners": "defer"},
                 "policies": {"readiness": {"require_approvals": 1}}})

    monkeypatch.setenv("RESULT_DIR", str(directory))
    monkeypatch.setenv("TARGET_GITHUB_TOKEN", "token")
    monkeypatch.setenv("PR_AUTOMATION_PLAN", json.dumps(plan))
    monkeypatch.setenv("REVIEWERS_CONFIG", "config/reviewers.yml")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    opener = FakeOpener(_routes())
    monkeypatch.setattr(
        report, "GitHubClient",
        lambda token, **kwargs: GitHubClient(token, opener=opener, sleep=lambda _: None),
    )
    return directory, opener, plan


def _comment_body(opener):
    return next(
        body["body"] for method, path, body in opener.calls
        if method == "POST" and path.endswith("/issues/7/comments")
    )


def _check_run(opener):
    return next(
        body for method, path, body in opener.calls
        if method == "POST" and path.endswith("/check-runs")
    )


# -- the happy path --------------------------------------------------------

def test_the_report_job_succeeds_and_writes_the_gate(artifacts):
    directory, _opener, _plan = artifacts
    assert report.main() == 0
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["overall"] == "PASS"
    assert gate["readiness"]["verdict"] == "READY"
    assert gate["readiness"]["ready"] is True


def test_the_gate_records_the_changed_line_coverage(artifacts):
    directory, _opener, _plan = artifacts
    report.main()
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["changed_line_coverage"] == 88.0
    assert gate["secrets_found"] == 0


def test_the_gate_records_the_reviewer_decision(artifacts):
    directory, _opener, _plan = artifacts
    report.main()
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    # CODEOWNERS covers only the backend file, so the frontend file is unowned.
    assert gate["reviewers"]["codeowners_users"] == ["backend-lead"]
    assert gate["reviewers"]["unowned_paths"] == ["frontend/src/App.tsx"]


def test_the_comment_carries_the_readiness_verdict_and_coverage(artifacts):
    _directory, opener, _plan = artifacts
    report.main()
    body = _comment_body(opener)
    assert "Merge readiness: READY" in body
    assert "**Changed-line coverage:** 88.0%" in body
    assert "@backend-lead" in body


def test_the_check_run_title_carries_both_verdicts(artifacts):
    _directory, opener, _plan = artifacts
    report.main()
    output = _check_run(opener)["output"]
    assert output["title"] == "READY · gate PASS"
    assert "Merge readiness: READY" in output["summary"]


def test_the_check_run_conclusion_follows_the_quality_gate(artifacts):
    _directory, opener, _plan = artifacts
    report.main()
    assert _check_run(opener)["conclusion"] == "success"


# -- readiness affects the message, not the conclusion ---------------------

def test_a_missing_approval_makes_it_not_ready_without_failing_the_check(artifacts):
    directory, opener, _plan = artifacts
    context = {**PR_CONTEXT, "approvals": [], "reviewed_by": []}
    (directory / "pr-context.json").write_text(json.dumps(context), encoding="utf-8")

    assert report.main() == 0
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["readiness"]["verdict"] == "NOT READY"
    # Branch protection already enforces approvals; failing here too would deadlock.
    assert _check_run(opener)["conclusion"] == "success"
    assert any("approval" in blocker for blocker in gate["readiness"]["blockers"])


def test_a_conflicted_merge_probe_blocks_readiness(artifacts):
    directory, _opener, _plan = artifacts
    (directory / "conflicts.json").write_text(
        json.dumps({"status": "conflicted", "conflict_count": 2, "conflicts": []}),
        encoding="utf-8",
    )
    report.main()
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["readiness"]["verdict"] == "NOT READY"
    assert any("conflicted file(s)" in blocker for blocker in gate["readiness"]["blockers"])


def test_a_committed_secret_fails_the_gate_and_blocks_readiness(artifacts):
    directory, opener, _plan = artifacts
    analysis = json.loads(json.dumps(ANALYSIS))
    analysis["secrets"]["data"] = {
        "status": "fail", "summary": "1 credential-shaped value(s) in added lines",
        "findings": [{"severity": "critical", "rule": "secret:aws-access-key-id",
                      "title": "AWS access key ID added", "detail": "Rotate it.",
                      "path": ".env", "line": 2}],
        "counts": {"critical": 1, "high": 0, "medium": 0, "low": 0, "info": 0},
        "hits": [{"rule": "aws-access-key-id", "provider": "AWS", "path": ".env", "line": 2,
                  "masked": "AK********** (20 chars)", "commented_out": False,
                  "severity": "critical", "title": "AWS access key ID", "entropy": 4.0}],
    }
    (directory / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")

    assert report.main() == 0
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["overall"] == "FAIL"
    assert gate["readiness"]["verdict"] == "NOT READY"
    assert gate["secrets_found"] == 1
    assert _check_run(opener)["conclusion"] == "failure"


def test_an_advisory_warning_keeps_the_check_green(artifacts):
    directory, opener, _plan = artifacts
    analysis = json.loads(json.dumps(ANALYSIS))
    analysis["size"]["data"] = {
        "status": "warn", "summary": "42 reviewable file(s), 900 changed line(s); risk high",
        "findings": [{"severity": "medium", "rule": "size-lines-large", "title": "900 lines",
                      "detail": "large"}],
        "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
    }
    (directory / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")

    report.main()
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["overall"] == "PASS"
    assert gate["readiness"]["verdict"] == "READY WITH WARNINGS"
    assert _check_run(opener)["conclusion"] == "success"


# -- missing artifacts -----------------------------------------------------

def test_a_missing_coverage_artifact_does_not_break_the_report(artifacts):
    directory, _opener, _plan = artifacts
    (directory / "coverage-result.json").unlink()
    assert report.main() == 0
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    assert gate["changed_line_coverage"] is None
    row = next(s for s in gate["sections"] if s["name"] == "Changed-line coverage")
    assert row["status"] == "NOT RUN"


def test_an_unreadable_artifact_is_ignored_rather_than_fatal(artifacts):
    directory, _opener, _plan = artifacts
    (directory / "coverage-result.json").write_text("{not json", encoding="utf-8")
    assert report.main() == 0


def test_a_missing_analysis_leaves_every_insight_row_not_run(artifacts):
    directory, _opener, _plan = artifacts
    (directory / "analysis.json").unlink()
    assert report.main() == 0
    gate = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
    statuses = {s["name"]: s["status"] for s in gate["sections"]}
    assert statuses["Secret scan"] == "NOT RUN"
    assert statuses["PR size"] == "NOT RUN"


def test_a_missing_pr_context_is_a_hard_failure(artifacts):
    directory, _opener, _plan = artifacts
    (directory / "pr-context.json").unlink()
    assert report.main() == 2
