"""The report job: new gate rows, readiness rendering, reviewer ownership."""
from __future__ import annotations

from scripts.common.dispatch import plan as build_plan
from scripts.common.readiness import assess
from scripts.github.report import (
    ERROR,
    FAIL,
    NOT_RUN,
    PASS,
    WARN,
    build_sections,
    overall,
    render_check_output,
    render_comment,
    render_insight_details,
    render_ownership,
    render_reviewers,
)

OK_QUALITY = {
    "status": "pass",
    "results": [
        {"name": "Deps", "group": "install", "status": "pass"},
        {"name": "Lint", "group": "lint", "status": "pass"},
        {"name": "Tests", "group": "test", "status": "pass"},
        {"name": "Build", "group": "build", "status": "pass"},
    ],
}

ANALYSIS = {
    "metadata": {"status": "ok", "data": {"summary": "s", "change_type": "fix", "risk": "low"}},
    "review": {"status": "ok", "data": {"findings": []}},
    "tests": {"status": "ok", "data": {"gaps": []}},
    "size": {"status": "ok", "data": {
        "status": "warn", "summary": "42 reviewable file(s), 900 changed line(s); risk high",
        "findings": [{"severity": "medium", "rule": "size-lines-large", "title": "900 lines",
                      "detail": "large"}],
        "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
    }},
    "secrets": {"status": "ok", "data": {
        "status": "fail", "summary": "1 credential-shaped value(s) in added lines",
        "findings": [{"severity": "critical", "rule": "secret:aws-access-key-id",
                      "title": "AWS access key ID added", "detail": "masked",
                      "path": ".env", "line": 3}],
        "counts": {"critical": 1, "high": 0, "medium": 0, "low": 0, "info": 0},
        "hits": [{"rule": "aws-access-key-id", "provider": "AWS", "path": ".env",
                  "line": 3, "masked": "AK********** (20 chars)", "commented_out": False,
                  "severity": "critical", "title": "AWS access key ID", "entropy": 3.9}],
    }},
    "dependencies": {"status": "ok", "data": {
        "status": "warn", "summary": "1 dependency change(s) across python",
        "findings": [{"severity": "medium", "rule": "deps-major-bump",
                      "title": "1 major version bump(s)", "detail": "django 4 -> 5"}],
        "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
        "packages": [{"name": "django", "kind": "changed", "from": "==4.2.11",
                      "to": "==5.0.3", "ecosystem": "python"}],
        "totals": {"major_bumps": 1},
    }},
    "migrations": {"status": "ok", "data": {
        "status": "pass", "summary": "0 migration file(s)", "findings": [],
        "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
    }},
    "api": {"status": "ok", "data": {
        "status": "pass", "summary": "No interface changes detected", "findings": [],
        "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
    }},
    "release_notes": {"status": "ok", "data": {
        "status": "pass", "summary": "2 note(s)",
        "markdown": "### Added\n\n- Add invoice export\n",
        "sections": [{"key": "features", "heading": "Added",
                      "entries": ["Add invoice export"], "dropped": 0}],
        "operator_actions": [],
    }},
}

COVERAGE_OK = {
    "status": "pass",
    "reason": "91.0% of 44 changed line(s) covered, at or above the 80% threshold.",
    "threshold": 80,
    "analysis": {
        "changed_line_coverage": 91.0, "measured_lines": 44, "covered_lines": 40,
        "uncovered_lines": 4, "denominator": 44,
        "files": [{"path": "app/api.py", "report_path": "", "added_lines": 50,
                   "measured_lines": 44, "covered_lines": 40, "uncovered_lines": [12, 13],
                   "uncovered_count": 4, "percent": 91.0, "not_executable": 6}],
        "unmeasured_files": [],
    },
}

COVERAGE_FAIL = {
    "status": "fail",
    "reason": "48.0% of 100 changed line(s) covered, under the 80% threshold.",
    "threshold": 80,
    "analysis": {"changed_line_coverage": 48.0, "measured_lines": 100, "covered_lines": 48,
                 "uncovered_lines": 52, "denominator": 100, "files": [],
                 "unmeasured_files": [{"path": "app/new.py", "added_lines": 12}]},
}

FLAKY_RESULT = {
    "status": "warn",
    "reason": "1 flaky test(s) and 0 consistent failure(s) across 3 run(s).",
    "runs": 3,
    "analysis": {
        "flaky": [{"identifier": "tests.test_a::test_two", "passes": 2, "failures": 1,
                   "errors": 0, "reason": "passed 2 time(s) and failed 1 time(s)",
                   "messages": ["AssertionError: timing"]}],
        "consistent_failures": [],
        "inconsistent_collection": [],
        "total_tests": 12,
    },
}


def _plan(action="all", **overrides):
    plan = dict(build_plan(action))
    plan.update(overrides)
    return plan


def _statuses(sections):
    return {s.name: s.status for s in sections}


# -- new gate rows ---------------------------------------------------------

def test_every_insight_gets_its_own_gate_row():
    sections, _data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK,
                                     FLAKY_RESULT)
    statuses = _statuses(sections)
    for name in ("Secret scan", "PR size", "Dependency risk", "Migrations",
                 "API compatibility", "Changed-line coverage", "Flaky tests"):
        assert name in statuses, name


def test_insight_statuses_map_onto_gate_statuses():
    sections, _data = build_sections(_plan(flaky=True), ANALYSIS, OK_QUALITY, None,
                                     COVERAGE_OK, FLAKY_RESULT)
    statuses = _statuses(sections)
    assert statuses["Secret scan"] == FAIL
    assert statuses["PR size"] == WARN
    assert statuses["Migrations"] == PASS
    assert statuses["Changed-line coverage"] == PASS
    assert statuses["Flaky tests"] == WARN


def test_a_flaky_result_is_ignored_when_flaky_detection_was_not_requested():
    # `all` excludes flaky, so a stale result file must not become a gate row.
    sections, _data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK,
                                    FLAKY_RESULT)
    assert _statuses(sections)["Flaky tests"] == NOT_RUN


def test_an_unrequested_insight_reads_as_not_run():
    sections, _data = build_sections(_plan("metadata"), ANALYSIS, None, None)
    statuses = _statuses(sections)
    assert statuses["Secret scan"] == NOT_RUN
    assert statuses["Changed-line coverage"] == NOT_RUN
    assert statuses["Flaky tests"] == NOT_RUN


def test_a_failed_insight_reads_as_error_not_as_a_pass():
    analysis = {**ANALYSIS, "size": {"status": "error", "error": "detector blew up"}}
    sections, _data = build_sections(_plan(), analysis, OK_QUALITY, None)
    row = next(s for s in sections if s.name == "PR size")
    assert row.status == ERROR
    assert "detector blew up" in row.detail


def test_a_requested_insight_with_no_result_reads_as_not_run():
    sections, _data = build_sections(_plan(), {}, None, None)
    assert _statuses(sections)["Secret scan"] == NOT_RUN


def test_high_severity_counts_are_added_to_the_row_detail():
    sections, _data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None)
    row = next(s for s in sections if s.name == "Secret scan")
    assert "1 high/critical finding(s)" in row.detail


def test_a_missing_coverage_result_is_not_run_rather_than_a_failure():
    sections, _data = build_sections(_plan("coverage"), ANALYSIS, OK_QUALITY, None, None)
    assert _statuses(sections)["Changed-line coverage"] == NOT_RUN


# -- how WARN affects the overall verdict ----------------------------------

def test_an_advisory_warning_does_not_degrade_the_overall_verdict():
    analysis = {**ANALYSIS, "secrets": {"status": "ok", "data": {
        "status": "pass", "summary": "clean", "findings": [], "counts": {}, "hits": [],
    }}}
    sections, _data = build_sections(_plan(), analysis, OK_QUALITY, None, COVERAGE_OK)
    # PR size and dependency risk are both WARN here.
    assert WARN in _statuses(sections).values()
    assert overall(sections) == PASS


def test_a_failing_insight_does_fail_the_overall_verdict():
    sections, _data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK)
    assert overall(sections) == FAIL


def test_a_coverage_breach_fails_the_gate():
    analysis = {**ANALYSIS, "secrets": {"status": "ok", "data": {
        "status": "pass", "summary": "clean", "findings": [], "counts": {}, "hits": [],
    }}}
    sections, _data = build_sections(_plan(), analysis, OK_QUALITY, None, COVERAGE_FAIL)
    assert _statuses(sections)["Changed-line coverage"] == FAIL
    assert overall(sections) == FAIL


# -- the data payload ------------------------------------------------------

def test_the_data_payload_carries_every_insight():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK,
                                     FLAKY_RESULT)
    assert set(data["insights"]) == {"secrets", "size", "deps", "migrations", "api"}
    assert data["coverage"]["analysis"]["changed_line_coverage"] == 91.0
    assert data["flaky"]["analysis"]["total_tests"] == 12
    assert data["release_notes"]["markdown"].startswith("### Added")


# -- reviewer rendering ----------------------------------------------------

def test_requested_reviewers_are_rendered_with_the_org_prefix_for_teams():
    line = render_reviewers("org/repo", {"users": ["alice"], "teams": ["backend"]})
    assert "@alice" in line and "@org/backend" in line


def test_codeowners_deference_is_explained():
    line = render_reviewers("org/repo", {
        "users": [], "teams": [], "deferred_to_codeowners": True,
        "suggested_users": ["owner"], "suggested_teams": [],
    })
    assert "CODEOWNERS covers every changed file" in line
    assert "@owner" in line


def test_codeowners_owners_are_shown_alongside_requested_reviewers():
    line = render_reviewers("org/repo", {
        "users": ["alice"], "teams": [], "suggested_users": ["owner"], "suggested_teams": [],
    })
    assert "requested @alice" in line
    assert "also owned by @owner" in line


def test_nothing_to_request_falls_back_to_the_notes():
    line = render_reviewers("org/repo", {
        "users": [], "teams": [], "notes": ["No reviewer group matched the changed files."],
    })
    assert "No reviewer group matched" in line


def test_ownership_lists_owners_and_unowned_paths():
    block = render_ownership({"ownership": {
        "per_owner": ["@backend-lead — 3 file(s)"],
        "unowned": ["docs/readme.md"],
        "matched_rules": ["/backend/"],
    }})
    assert "@backend-lead — 3 file(s)" in block
    assert "docs/readme.md" in block
    assert "`/backend/`" in block


def test_ownership_is_empty_when_there_is_no_codeowners_file():
    assert render_ownership({"ownership": {}}) == ""


# -- insight detail blocks -------------------------------------------------

def test_insight_details_render_each_section_that_found_something():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK,
                                     FLAKY_RESULT)
    block = render_insight_details(data)
    assert "Detected credentials (1)" in block
    assert "Dependency changes (1)" in block
    assert "Size and risk (1)" in block
    # Nothing found means no block at all, rather than an empty one.
    assert "Migration checks" not in block


def test_the_credential_block_says_to_rotate_and_never_shows_the_value():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None)
    block = render_insight_details(data)
    assert "Rotate every credential" in block
    assert "AK********** (20 chars)" not in block or "masked" in block.lower()


def test_uncovered_changed_lines_are_listed():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK)
    block = render_insight_details(data)
    assert "Changed lines with no test coverage" in block
    assert "`app/api.py`" in block


def test_unmeasured_files_are_listed_separately():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_FAIL)
    block = render_insight_details(data)
    assert "absent from every coverage report" in block
    assert "app/new.py" in block


def test_flaky_tests_are_listed():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, None, FLAKY_RESULT)
    block = render_insight_details(data)
    assert "Flaky tests (1)" in block
    assert "tests.test_a::test_two" in block


def test_the_draft_release_notes_are_included():
    _sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None)
    assert "Draft release notes" in render_insight_details(data)


# -- the comment -----------------------------------------------------------

def _comment(coverage=COVERAGE_OK, flaky=FLAKY_RESULT, reviewers=None, context=None):
    plan = _plan()
    sections, data = build_sections(plan, ANALYSIS, OK_QUALITY, None, coverage, flaky)
    verdict = overall(sections)
    readiness = assess(
        sections=[{"name": s.name, "status": s.status, "detail": s.detail} for s in sections],
        context=context or {"approvals": ["alice"], "mergeable": True,
                            "mergeable_state": "clean"},
    )
    return render_comment(
        "org/repo", 7, sections, data, ["fix"],
        reviewers or {"users": ["alice"], "teams": [], "notes": []},
        verdict, readiness=readiness,
    )


def test_the_comment_leads_with_the_readiness_verdict():
    body = _comment()
    assert "Merge readiness: NOT READY" in body
    # The readiness block comes before the check table.
    assert body.index("Merge readiness") < body.index("All checks")


def test_the_comment_states_the_changed_line_coverage():
    assert "**Changed-line coverage:** 91.0% (40/44 line(s))" in _comment()


def test_the_comment_names_what_is_blocking():
    body = _comment()
    assert "Secret scan" in body
    assert "🛑" in body


def test_the_comment_collapses_the_full_check_table():
    body = _comment()
    assert "<details><summary>All checks (" in body


def test_the_comment_still_says_human_approval_is_required():
    assert "Human approval is still required before merge." in _comment()


def test_the_comment_includes_the_ownership_block_when_there_is_one():
    body = _comment(reviewers={
        "users": [], "teams": [], "notes": [], "deferred_to_codeowners": True,
        "suggested_users": ["owner"], "suggested_teams": [],
        "ownership": {"per_owner": ["@owner — 4 file(s)"], "unowned": [],
                      "matched_rules": ["*"]},
    })
    assert "File ownership (1 owner(s))" in body


def test_a_comment_without_readiness_still_renders():
    plan = _plan()
    sections, data = build_sections(plan, ANALYSIS, OK_QUALITY, None)
    body = render_comment("org/repo", 7, sections, data, [], {"users": [], "teams": []},
                          overall(sections))
    assert "PR Automation Report" in body
    assert "Merge readiness" not in body


# -- the check run output --------------------------------------------------

def test_the_check_output_appends_the_readiness_breakdown():
    sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None, COVERAGE_OK)
    readiness = assess(
        sections=[{"name": s.name, "status": s.status, "detail": s.detail} for s in sections],
        context={"approvals": ["alice"], "mergeable": True, "mergeable_state": "clean"},
    )
    text = render_check_output(sections, data["counts"], overall(sections), readiness)
    assert "Merge readiness: NOT READY" in text
    assert "Secret scan: BLOCKING" in text


def test_the_check_output_is_unchanged_without_readiness():
    sections, data = build_sections(_plan(), ANALYSIS, OK_QUALITY, None)
    text = render_check_output(sections, data["counts"], overall(sections))
    assert "Merge readiness" not in text
    assert text.startswith("PR Automation / Quality Gate")
