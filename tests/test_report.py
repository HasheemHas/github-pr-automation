"""Gate rendering, missing optional artifacts and PR head race detection."""
from __future__ import annotations

import json

import pytest

from scripts.common.dispatch import plan as build_plan
from scripts.common.errors import AutomationError, GitHubError
from scripts.github.api import GitHubClient
from scripts.github.report import (
    FAIL,
    NOT_RUN,
    PARTIAL,
    PASS,
    Reporter,
    build_sections,
    overall,
    render_check_output,
    upsert_summary,
)
from tests.conftest import FakeOpener

OK_QUALITY = {
    "status": "pass",
    "results": [
        {"name": "Deps", "group": "install", "status": "pass"},
        {"name": "Lint", "group": "lint", "status": "pass"},
        {"name": "Tests", "group": "test", "status": "pass"},
        {"name": "Build", "group": "build", "status": "pass"},
    ],
}

ANALYSIS_OK = {
    "metadata": {"status": "ok", "data": {"summary": "s", "change_type": "fix", "risk": "low"}},
    "review": {"status": "ok", "data": {"findings": []}},
    "tests": {"status": "ok", "data": {"gaps": []}},
}


def statuses(sections):
    return {s.name: s.status for s in sections}


# -- three distinct outcomes for optional results --------------------------

def test_a_skipped_validation_reads_as_not_run():
    plan = dict(build_plan("all"))
    sections, _ = build_sections(plan, ANALYSIS_OK, None, None)
    assert statuses(sections)["Lint"] == NOT_RUN
    assert statuses(sections)["Security"] == NOT_RUN


def test_a_failed_validation_reads_as_fail():
    quality = {
        "status": "fail",
        "results": [{"name": "Lint", "group": "lint", "status": "fail"}],
    }
    sections, _ = build_sections(dict(build_plan("all")), ANALYSIS_OK, quality, None)
    assert statuses(sections)["Lint"] == FAIL


def test_a_crashed_validation_that_produced_no_artifact_reads_as_not_run_not_fail():
    # This is the case that previously crashed the report job.
    sections, _ = build_sections(dict(build_plan("quality")), None, None, None)
    assert statuses(sections)["Lint"] == NOT_RUN
    assert overall(sections) == NOT_RUN


def test_an_install_failure_surfaces_as_dependencies_fail_and_gate_fail():
    quality = {
        "status": "fail",
        "results": [
            {"name": "Frontend dependencies", "group": "install", "status": "fail"},
            {"name": "Frontend lint", "group": "lint", "status": "skipped",
             "reason": "Skipped because Frontend dependencies failed."},
        ],
    }
    sections, _ = build_sections(dict(build_plan("quality")), None, quality, None)
    assert statuses(sections)["Dependencies"] == FAIL
    assert statuses(sections)["Lint"] == NOT_RUN
    assert overall(sections) == FAIL


def test_a_quality_config_error_reads_as_error():
    quality = {"status": "error", "reason": "bad config", "results": []}
    sections, _ = build_sections(dict(build_plan("quality")), None, quality, None)
    assert statuses(sections)["Lint"] == "ERROR"


def test_a_group_with_no_configured_commands_is_not_run():
    quality = {"status": "pass", "results": [{"name": "Lint", "group": "lint", "status": "pass"}]}
    sections, _ = build_sections(dict(build_plan("quality")), None, quality, None)
    assert statuses(sections)["Test"] == NOT_RUN
    assert statuses(sections)["Lint"] == PASS


# -- AI failure never reads as success ------------------------------------

def test_an_ai_error_reads_as_error_and_yields_no_metadata_to_write():
    analysis = {"metadata": {"status": "error", "error": "provider down", "data": None}}
    sections, data = build_sections(dict(build_plan("metadata")), analysis, None, None)
    assert statuses(sections)["Metadata"] == "ERROR"
    assert data["metadata"] is None


def test_an_absent_analysis_artifact_reads_as_not_run():
    sections, data = build_sections(dict(build_plan("metadata")), None, None, None)
    assert statuses(sections)["Metadata"] == NOT_RUN
    assert data["metadata"] is None


def test_a_step_not_requested_reads_as_not_run():
    sections, _ = build_sections(dict(build_plan("labels")), None, None, None)
    assert statuses(sections)["Metadata"] == NOT_RUN
    assert "Not requested" in {s.name: s.detail for s in sections}["Metadata"]


# -- verdict ---------------------------------------------------------------

def test_critical_findings_fail_the_gate():
    analysis = {
        "review": {
            "status": "ok",
            "data": {"findings": [{"severity": "critical", "title": "t", "path": "a.py"}]},
        }
    }
    sections, data = build_sections(dict(build_plan("review")), analysis, None, None)
    assert statuses(sections)["Review"] == FAIL
    assert overall(sections) == FAIL
    assert data["counts"]["critical"] == 1


def test_medium_and_low_findings_do_not_fail_the_gate():
    analysis = {
        "review": {
            "status": "ok",
            "data": {
                "findings": [
                    {"severity": "medium", "title": "t", "path": "a.py"},
                    {"severity": "low", "title": "u", "path": "a.py"},
                ]
            },
        }
    }
    sections, _ = build_sections(dict(build_plan("review")), analysis, None, None)
    assert statuses(sections)["Review"] == PASS
    assert overall(sections) == PASS


def test_an_error_section_makes_the_run_partial_not_failed():
    analysis = {"review": {"status": "error", "error": "x", "data": None}}
    sections, _ = build_sections(dict(build_plan("review")), analysis, None, None)
    assert overall(sections) == PARTIAL


def test_a_full_pass_run():
    security = {"status": "pass", "results": [{"name": "gitleaks", "status": "pass"}]}
    sections, _ = build_sections(dict(build_plan("all")), ANALYSIS_OK, OK_QUALITY, security)
    assert overall(sections) == PASS
    text = render_check_output(sections, {"critical": 0, "high": 0}, PASS)
    for label in ("Metadata", "Review", "Security", "Lint", "Test", "Build", "Overall"):
        assert label in text
    assert "PASS" in text


def test_a_partly_provisioned_scanner_set_is_reported_as_partial():
    security = {"status": "partial", "reason": "trivy missing", "results": []}
    sections, _ = build_sections(dict(build_plan("security")), ANALYSIS_OK, None, security)
    assert statuses(sections)["Security"] == PARTIAL


# -- PR body handling ------------------------------------------------------

def test_the_managed_summary_block_replaces_only_itself():
    body = "Author text.\n\n<!-- ai-pr-summary:start -->\nold\n<!-- ai-pr-summary:end -->"
    updated = upsert_summary(body, "new summary", {"change_type": "fix", "risk": "low"})
    assert "Author text." in updated
    assert "old" not in updated
    assert updated.count("<!-- ai-pr-summary:start -->") == 1
    assert "new summary" in updated


def test_an_empty_body_gets_only_the_managed_block():
    updated = upsert_summary("", "s", {})
    assert updated.startswith("<!-- ai-pr-summary:start -->")


def test_recommended_testing_is_rendered_when_present():
    updated = upsert_summary("", "s", {"recommended_testing": ["run pytest"]})
    assert "- run pytest" in updated


# -- head race detection ---------------------------------------------------

def _reporter(routes):
    opener = FakeOpener(routes)
    client = GitHubClient("token", opener=opener, sleep=lambda _: None)
    return Reporter(client, "org", "repo", 7), opener


def test_writes_proceed_when_the_head_still_matches():
    head = "a" * 40
    reporter, _ = _reporter(
        {"GET /repos/org/repo/pulls/7": ({"state": "open", "head": {"sha": head}}, {})}
    )
    reporter.verify_head(head)  # does not raise


def test_a_moved_head_aborts_every_write():
    reporter, opener = _reporter(
        {"GET /repos/org/repo/pulls/7": ({"state": "open", "head": {"sha": "b" * 40}}, {})}
    )
    with pytest.raises(AutomationError, match="head moved"):
        reporter.verify_head("a" * 40)
    # Only the verification GET happened; nothing was written.
    assert [c[0] for c in opener.calls] == ["GET"]


def test_a_closed_pr_aborts_every_write():
    reporter, _ = _reporter(
        {"GET /repos/org/repo/pulls/7": ({"state": "closed", "head": {"sha": "a" * 40}}, {})}
    )
    with pytest.raises(AutomationError, match="no longer open"):
        reporter.verify_head("a" * 40)


# -- reviewer request degradation -----------------------------------------

def test_reviewer_requests_fall_back_to_one_at_a_time_on_422():
    import urllib.error

    class Opener:
        def __init__(self):
            self.bodies = []

        def __call__(self, request, timeout=None):
            body = json.loads(request.data.decode())
            self.bodies.append(body)
            reviewers = body.get("reviewers") or []
            if len(reviewers) != 1 or reviewers == ["ghost"]:
                raise urllib.error.HTTPError(request.full_url, 422, "unprocessable", {}, None)
            from tests.conftest import FakeResponse

            return FakeResponse(b"{}")

    opener = Opener()
    client = GitHubClient("token", opener=opener, sleep=lambda _: None)
    reporter = Reporter(client, "org", "repo", 7)
    accepted = reporter.request_reviewers(["real", "ghost"], [])
    assert accepted == {"users": ["real"], "teams": []}
    assert any("ghost" in note for note in reporter.notes)


def test_no_reviewers_means_no_request_is_sent():
    reporter, opener = _reporter({})
    assert reporter.request_reviewers([], []) == {"users": [], "teams": []}
    assert opener.calls == []


def test_a_non_422_reviewer_error_is_not_swallowed():
    import urllib.error

    class Opener:
        def __call__(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

    client = GitHubClient("token", opener=Opener(), sleep=lambda _: None, retries=1)
    reporter = Reporter(client, "org", "repo", 7)
    with pytest.raises(GitHubError):
        reporter.request_reviewers(["real"], [])


# -- check run -------------------------------------------------------------

@pytest.mark.parametrize(
    "verdict,conclusion",
    [(PASS, "success"), (FAIL, "failure"), (PARTIAL, "neutral"), (NOT_RUN, "neutral")],
)
def test_the_check_is_published_against_the_target_head_sha(verdict, conclusion):
    head = "c" * 40
    reporter, opener = _reporter({"POST /repos/org/repo/check-runs": ({}, {})})
    reporter.publish_check(head, verdict, "text", "summary")
    _, path, body = opener.calls[0]
    assert path == "/repos/org/repo/check-runs"
    assert body["head_sha"] == head
    assert body["conclusion"] == conclusion
    assert body["name"] == "PR Automation / Quality Gate"
