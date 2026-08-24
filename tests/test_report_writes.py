"""Title replacement, inline review comments and PARTIAL propagation."""
from __future__ import annotations

import json

import pytest

from scripts.common.dispatch import plan as build_plan
from scripts.github.api import GitHubClient
from scripts.github.report import (
    FAIL,
    NOT_RUN,
    PARTIAL,
    PASS,
    Reporter,
    Section,
    build_sections,
    finding_key,
    overall,
    render_comment,
    render_inline_comment,
)
from tests.conftest import FakeOpener

HEAD = "a" * 40


def _reporter(routes):
    opener = FakeOpener(routes)
    return Reporter(GitHubClient("token", opener=opener, sleep=lambda _: None),
                    "org", "repo", 7), opener


# -- PARTIAL propagation ---------------------------------------------------

def test_a_partial_security_section_makes_the_overall_verdict_partial():
    # Previously this whole set collapsed to PASS, hiding a scanner that never ran.
    sections = [
        Section("Metadata", PASS),
        Section("Review", PASS),
        Section("Security", PARTIAL, "trivy not provisioned"),
        Section("Lint", PASS),
        Section("Test", PASS),
        Section("Build", PASS),
    ]
    assert overall(sections) == PARTIAL


def test_fail_still_beats_partial():
    assert overall([Section("Security", PARTIAL), Section("Lint", FAIL)]) == FAIL


def test_partial_beats_pass_and_error_still_maps_to_partial():
    assert overall([Section("Review", "ERROR"), Section("Lint", PASS)]) == PARTIAL
    assert overall([Section("Lint", PASS), Section("Build", PASS)]) == PASS


def test_all_not_run_stays_not_run():
    assert overall([Section("Lint", NOT_RUN), Section("Build", NOT_RUN)]) == NOT_RUN


def test_an_unprovisioned_scanner_propagates_end_to_end():
    security = {"status": "partial", "reason": "trivy is not installed", "results": []}
    quality = {
        "status": "pass",
        "results": [
            {"name": "Deps", "group": "install", "status": "pass"},
            {"name": "Lint", "group": "lint", "status": "pass"},
            {"name": "Tests", "group": "test", "status": "pass"},
            {"name": "Build", "group": "build", "status": "pass"},
        ],
    }
    analysis = {
        "metadata": {"status": "ok", "data": {"summary": "s"}},
        "review": {"status": "ok", "data": {"findings": [], "complete": True, "batches": 1}},
        "tests": {"status": "ok", "data": {"gaps": []}},
    }
    sections, _ = build_sections(dict(build_plan("all")), analysis, quality, security)
    assert overall(sections) == PARTIAL


# -- incomplete review coverage -------------------------------------------

def test_an_incomplete_review_is_partial_not_pass():
    analysis = {
        "review": {
            "status": "ok",
            "data": {
                "findings": [],
                "batches": 5,
                "succeeded_batches": 5,
                "complete": False,
                "unreviewed_paths": ["big/one.py", "big/two.py"],
            },
        }
    }
    sections, _ = build_sections(dict(build_plan("review")), analysis, None, None)
    review = {s.name: s for s in sections}["Review"]
    assert review.status == PARTIAL
    assert "2 file(s) not reviewed" in review.detail
    assert overall(sections) == PARTIAL


def test_a_complete_multi_batch_review_passes():
    analysis = {
        "review": {
            "status": "ok",
            "data": {"findings": [], "batches": 4, "succeeded_batches": 4, "complete": True},
        }
    }
    sections, _ = build_sections(dict(build_plan("review")), analysis, None, None)
    review = {s.name: s for s in sections}["Review"]
    assert review.status == PASS
    assert "4/4 batch(es)" in review.detail


def test_findings_still_fail_an_incomplete_review():
    analysis = {
        "review": {
            "status": "ok",
            "data": {
                "findings": [{"severity": "critical", "title": "t", "path": "a.py"}],
                "batches": 2,
                "complete": False,
                "unreviewed_paths": ["b.py"],
            },
        }
    }
    sections, _ = build_sections(dict(build_plan("review")), analysis, None, None)
    assert {s.name: s.status for s in sections}["Review"] == FAIL


# -- title replacement ----------------------------------------------------

def test_a_weak_title_is_replaced_on_the_pull_request():
    reporter, opener = _reporter(
        {
            "GET /repos/org/repo/pulls/7": ({"title": "update", "body": "text"}, {}),
            "PATCH /repos/org/repo/pulls/7": ({}, {}),
        }
    )
    outcome = reporter.update_metadata(
        {"summary": "Adds refresh-token validation.",
         "title": "feat(auth): add refresh-token validation"}
    )
    assert outcome["title_updated"] is True
    patch_body = [c for c in opener.calls if c[0] == "PATCH"][0][2]
    assert patch_body["title"] == "feat(auth): add refresh-token validation"
    assert "AI PR Summary" in patch_body["body"]


def test_a_good_title_is_left_alone_but_the_body_is_still_updated():
    reporter, opener = _reporter(
        {
            "GET /repos/org/repo/pulls/7": (
                {"title": "fix(auth): prevent refresh token reuse", "body": ""}, {}
            ),
            "PATCH /repos/org/repo/pulls/7": ({}, {}),
        }
    )
    outcome = reporter.update_metadata(
        {"summary": "s", "title": "feat(auth): rework token handling"}
    )
    assert outcome["title_updated"] is False
    patch_body = [c for c in opener.calls if c[0] == "PATCH"][0][2]
    assert "title" not in patch_body
    assert "AI PR Summary" in patch_body["body"]


def test_the_never_policy_leaves_a_placeholder_title_alone():
    reporter, opener = _reporter(
        {
            "GET /repos/org/repo/pulls/7": ({"title": "update", "body": ""}, {}),
            "PATCH /repos/org/repo/pulls/7": ({}, {}),
        }
    )
    outcome = reporter.update_metadata({"summary": "s", "title": "feat: real title here"},
                                      title_policy="never")
    assert outcome["title_updated"] is False
    assert "title" not in [c for c in opener.calls if c[0] == "PATCH"][0][2]


def test_the_always_policy_replaces_a_good_title():
    reporter, opener = _reporter(
        {
            "GET /repos/org/repo/pulls/7": (
                {"title": "fix(auth): prevent reuse", "body": ""}, {}
            ),
            "PATCH /repos/org/repo/pulls/7": ({}, {}),
        }
    )
    outcome = reporter.update_metadata(
        {"summary": "s", "title": "fix(auth): validate token expiry"}, title_policy="always"
    )
    assert outcome["title_updated"] is True


def test_no_summary_means_no_write_at_all():
    reporter, opener = _reporter({})
    outcome = reporter.update_metadata({"summary": "", "title": "feat: something real"})
    assert outcome == {
        "body_updated": False,
        "title_updated": False,
        "title_reason": "No AI summary, so nothing was written.",
    }
    assert opener.calls == []


def test_the_decision_reason_is_recorded_for_the_report():
    reporter, _ = _reporter(
        {
            "GET /repos/org/repo/pulls/7": ({"title": "update", "body": ""}, {}),
            "PATCH /repos/org/repo/pulls/7": ({}, {}),
        }
    )
    reporter.update_metadata({"summary": "s", "title": "feat(auth): add validation"})
    assert any("Title replaced" in note for note in reporter.notes)


# -- inline review comments ------------------------------------------------

FINDING = {
    "severity": "high",
    "category": "security",
    "path": "backend/app/auth.py",
    "line": 74,
    "title": "Token expiry is not validated",
    "body": "Compare exp against the current time.",
}


def test_inline_comments_are_posted_as_a_review_on_the_head_commit():
    reporter, opener = _reporter(
        {
            "GET /repos/org/repo/pulls/7/comments?per_page=100": ([], {}),
            "POST /repos/org/repo/pulls/7/reviews": ({}, {}),
        }
    )
    outcome = reporter.post_inline_review([FINDING], HEAD)
    assert outcome["posted"] == 1
    body = [c for c in opener.calls if c[0] == "POST"][0][2]
    assert body["commit_id"] == HEAD
    assert body["event"] == "COMMENT"
    comment = body["comments"][0]
    assert comment == {
        "path": "backend/app/auth.py",
        "line": 74,
        "side": "RIGHT",
        "body": render_inline_comment(FINDING),
    }


def test_the_comment_names_the_severity_and_the_finding():
    rendered = render_inline_comment(FINDING)
    assert "HIGH" in rendered
    assert "Token expiry is not validated" in rendered
    assert "Compare exp against the current time." in rendered
    assert finding_key(FINDING) in rendered


def test_no_findings_means_no_review_is_posted():
    reporter, opener = _reporter({})
    assert reporter.post_inline_review([], HEAD)["posted"] == 0
    assert opener.calls == []


def test_a_finding_already_commented_on_is_not_repeated():
    existing = [{"body": f"<!-- ai-finding:{finding_key(FINDING)} -->\nold"}]
    reporter, opener = _reporter(
        {"GET /repos/org/repo/pulls/7/comments?per_page=100": (existing, {})}
    )
    outcome = reporter.post_inline_review([FINDING], HEAD)
    assert outcome == {"posted": 0, "skipped_existing": 1, "rejected": 0, "overflow": []}
    assert not [c for c in opener.calls if c[0] == "POST"]


def test_findings_beyond_the_cap_stay_in_the_summary():
    findings = [{**FINDING, "line": 70 + i, "title": f"Issue {i}"} for i in range(30)]
    reporter, opener = _reporter(
        {
            "GET /repos/org/repo/pulls/7/comments?per_page=100": ([], {}),
            "POST /repos/org/repo/pulls/7/reviews": ({}, {}),
        }
    )
    outcome = reporter.post_inline_review(findings, HEAD, max_comments=5)
    assert outcome["posted"] == 5
    assert len(outcome["overflow"]) == 25
    assert len([c for c in opener.calls if c[0] == "POST"][0][2]["comments"]) == 5


def test_a_rejected_comment_falls_back_to_one_at_a_time():
    import urllib.error

    class Opener:
        def __init__(self):
            self.posts = 0

        def __call__(self, request, timeout=None):
            from tests.conftest import FakeResponse

            if request.get_method() == "GET":
                return FakeResponse(b"[]")
            body = json.loads(request.data.decode())
            self.posts += 1
            # The batch fails; single comments succeed except the second one.
            if len(body["comments"]) > 1:
                raise urllib.error.HTTPError(request.full_url, 422, "bad", {}, None)
            if body["comments"][0]["line"] == 99:
                raise urllib.error.HTTPError(request.full_url, 422, "bad", {}, None)
            return FakeResponse(b"{}")

    opener = Opener()
    reporter = Reporter(
        GitHubClient("token", opener=opener, sleep=lambda _: None), "org", "repo", 7
    )
    outcome = reporter.post_inline_review(
        [FINDING, {**FINDING, "line": 99, "title": "bad anchor"}], HEAD
    )
    assert outcome["posted"] == 1
    assert outcome["rejected"] == 1
    assert any("rejected an inline comment" in note for note in reporter.notes)


def test_a_non_422_review_error_is_not_swallowed():
    import urllib.error

    from scripts.common.errors import GitHubError

    class Opener:
        def __call__(self, request, timeout=None):
            from tests.conftest import FakeResponse

            if request.get_method() == "GET":
                return FakeResponse(b"[]")
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

    reporter = Reporter(
        GitHubClient("token", opener=Opener(), sleep=lambda _: None, retries=1),
        "org", "repo", 7,
    )
    with pytest.raises(GitHubError):
        reporter.post_inline_review([FINDING], HEAD)


def test_de_duplication_failure_does_not_drop_the_finding():
    import urllib.error

    class Opener:
        def __call__(self, request, timeout=None):
            from tests.conftest import FakeResponse

            if request.get_method() == "GET":
                raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)
            return FakeResponse(b"{}")

    reporter = Reporter(
        GitHubClient("token", opener=Opener(), sleep=lambda _: None, retries=1),
        "org", "repo", 7,
    )
    assert reporter.post_inline_review([FINDING], HEAD)["posted"] == 1
    assert any("skipped de-duplication" in note for note in reporter.notes)


def test_the_fingerprint_changes_with_the_line_and_title():
    assert finding_key(FINDING) != finding_key({**FINDING, "line": 75})
    assert finding_key(FINDING) != finding_key({**FINDING, "title": "Other"})
    assert finding_key(FINDING) == finding_key(dict(FINDING))


# -- report rendering -----------------------------------------------------

def _render(**kwargs):
    data = {
        "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
        "findings": [FINDING],
        "gaps": [],
        "metadata": {"summary": "s"},
        "review": {"batches": 3, "succeeded_batches": 3, "complete": True,
                   "reviewed_paths": ["a.py", "b.py"]},
        "tests": {},
    }
    defaults = {
        "full_name": "org/repo",
        "number": 7,
        "sections": [Section("Review", FAIL, "1 finding")],
        "data": data,
        "labels": ["backend"],
        "reviewers": {"users": [], "teams": [], "notes": []},
        "verdict": FAIL,
    }
    defaults.update(kwargs)
    return render_comment(
        defaults.pop("full_name"),
        defaults.pop("number"),
        defaults.pop("sections"),
        defaults.pop("data"),
        defaults.pop("labels"),
        defaults.pop("reviewers"),
        defaults.pop("verdict"),
        **defaults,
    )


def test_the_report_states_the_title_decision():
    body = _render(
        metadata_outcome={"title_updated": True, "new_title": "feat: real title",
                          "title_reason": "Existing title 'update' is a placeholder."}
    )
    assert "replaced with `feat: real title`" in body


def test_the_report_states_when_the_title_was_kept():
    body = _render(metadata_outcome={"title_updated": False,
                                     "title_reason": "Existing title is descriptive; kept."})
    assert "unchanged" in body


def test_the_report_states_the_inline_comment_outcome():
    body = _render(
        inline_outcome={"posted": 2, "skipped_existing": 1, "rejected": 0, "overflow": ["x"]},
        unanchored=[{"title": "y"}],
    )
    assert "2 posted inline" in body
    assert "1 already commented" in body
    assert "1 over the inline cap" in body
    assert "1 not anchorable to a diff line" in body


def test_the_report_states_review_coverage():
    body = _render()
    assert "Review coverage:" in body
    assert "3/3 batch(es)" in body


def test_the_report_names_unreviewed_files():
    data_review = {"batches": 4, "succeeded_batches": 3, "complete": False,
                   "reviewed_paths": ["a.py"], "unreviewed_paths": ["big.py"]}
    body = _render(
        data={
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [], "gaps": [], "metadata": None, "tests": {},
            "review": data_review,
        }
    )
    assert "Not reviewed" in body
    assert "`big.py`" in body
