"""Pagination, retries and PR context assembly. No real network access."""
from __future__ import annotations

import json

import pytest

from scripts.common.errors import AutomationError, GitHubError, LimitExceeded
from scripts.github.api import GitHubClient
from scripts.github.get_pr import build_context, fetch_codeowners
from tests.conftest import FakeOpener

PR = {
    "state": "open",
    "title": "Add thing",
    "body": "Body",
    "user": {"login": "dev"},
    "draft": False,
    "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": "org/repo"}},
    "base": {"sha": "b" * 40, "ref": "main"},
    "mergeable": True,
    "mergeable_state": "clean",
    "requested_reviewers": [{"login": "existing"}],
    "requested_teams": [{"slug": "existing-team"}],
}


def _client(routes):
    opener = FakeOpener(routes)
    return GitHubClient("token", opener=opener, sleep=lambda _: None), opener


def _files(count, start=0):
    return [{"filename": f"f{i}.py", "status": "modified", "additions": 1, "deletions": 0}
            for i in range(start, start + count)]


def test_pagination_follows_the_link_header():
    page_one = (
        _files(2),
        {"Link": '<https://api.github.com/repos/org/repo/pulls/1/files?page=2&per_page=100>; rel="next"'},
    )
    client, opener = _client(
        {
            "GET /repos/org/repo/pulls/1/files?per_page=100": page_one,
            "GET /repos/org/repo/pulls/1/files?page=2&per_page=100": (_files(1, 2), {}),
        }
    )
    items = client.paginate("/repos/org/repo/pulls/1/files")
    assert [i["filename"] for i in items] == ["f0.py", "f1.py", "f2.py"]
    assert len(opener.calls) == 2


def test_a_single_page_without_a_link_header_stops():
    client, opener = _client({"GET /repos/org/repo/tags?per_page=100": (["v1.0.0"], {})})
    assert client.paginate("/repos/org/repo/tags") == ["v1.0.0"]
    assert len(opener.calls) == 1


def test_a_lowercase_link_header_is_honoured():
    client, _ = _client(
        {
            "GET /x?per_page=100": (
                [1],
                {"link": '<https://api.github.com/x?page=2&per_page=100>; rel="next"'},
            ),
            "GET /x?page=2&per_page=100": ([2], {}),
        }
    )
    assert client.paginate("/x") == [1, 2]


def test_a_query_string_is_preserved_when_adding_per_page():
    client, opener = _client({"GET /x?state=open&per_page=100": ([], {})})
    client.paginate("/x?state=open")
    assert opener.calls[0][1] == "/x?state=open&per_page=100"


def test_exceeding_the_configured_limit_reports_rather_than_truncating():
    client, _ = _client({"GET /repos/org/repo/pulls/1/files?per_page=100": (_files(5), {})})
    with pytest.raises(LimitExceeded) as excinfo:
        client.paginate("/repos/org/repo/pulls/1/files", limit=3, label="changed files")
    assert "PR exceeds configured analysis limit" in str(excinfo.value)


def test_a_retryable_status_is_retried():
    import urllib.error

    class Flaky:
        def __init__(self):
            self.calls = 0

        def __call__(self, request, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
            from tests.conftest import FakeResponse

            return FakeResponse(json.dumps({"ok": True}).encode())

    flaky = Flaky()
    client = GitHubClient("token", opener=flaky, sleep=lambda _: None)
    assert client.get("/x") == {"ok": True}
    assert flaky.calls == 2


def test_a_non_retryable_status_fails_immediately():
    import urllib.error

    class Failing:
        calls = 0

        def __call__(self, request, timeout=None):
            Failing.calls += 1
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    client = GitHubClient("token", opener=Failing(), sleep=lambda _: None)
    with pytest.raises(GitHubError) as excinfo:
        client.get("/x")
    assert excinfo.value.status == 404
    assert Failing.calls == 1


def test_the_token_is_sent_as_a_header_and_never_in_the_url():
    client, opener = _client({"GET /x": ({}, {})})
    client.get("/x")
    method, path, _ = opener.calls[0]
    assert "token" not in path
    assert method == "GET"


def test_an_empty_token_is_refused():
    with pytest.raises(GitHubError, match="installation token is required"):
        GitHubClient("")


# -- PR context ------------------------------------------------------------

def _context_routes(files=None):
    return {
        "GET /repos/org/repo/pulls/1": (PR, {}),
        "GET /repos/org/repo/pulls/1/files?per_page=100": (files or _files(2), {}),
        "GET /repos/org/repo/pulls/1/commits?per_page=100": ([{"commit": {"message": "Add thing\n\nbody"}}], {}),
        "GET /repos/org/repo/pulls/1/comments?per_page=100": ([], {}),
        "GET /repos/org/repo/issues/1/comments?per_page=100": ([{"body": "hi"}], {}),
        "GET /repos/org/repo/issues/1/labels?per_page=100": ([{"name": "backend"}], {}),
        "GET /repos/org/repo/pulls/1/reviews?per_page=100": (
            [{"user": {"login": "approver"}, "state": "APPROVED"}], {},
        ),
    }


def test_build_context_collects_every_paginated_collection():
    client, _ = _client(_context_routes())
    context = build_context(client, "org/repo", 1, {})
    assert context["files"] == ["f0.py", "f1.py"]
    assert context["commits"] == ["Add thing"]
    assert context["issue_comment_count"] == 1
    assert context["labels"] == ["backend"]
    assert context["existing_reviewers"] == ["existing"]
    assert context["existing_reviewer_teams"] == ["existing-team"]
    assert context["from_fork"] is False
    assert context["approvals"] == ["approver"]
    assert context["changes_requested"] == []


def test_build_context_flags_a_fork():
    forked = json.loads(json.dumps(PR))
    forked["head"]["repo"]["full_name"] = "someone/repo"
    routes = _context_routes()
    routes["GET /repos/org/repo/pulls/1"] = (forked, {})
    client, _ = _client(routes)
    assert build_context(client, "org/repo", 1, {})["from_fork"] is True


def test_build_context_refuses_a_closed_pr():
    closed = json.loads(json.dumps(PR))
    closed["state"] = "closed"
    client, _ = _client({"GET /repos/org/repo/pulls/1": (closed, {})})
    with pytest.raises(AutomationError, match="is not open"):
        build_context(client, "org/repo", 1, {})


def test_build_context_refuses_an_invalid_head_sha():
    bad = json.loads(json.dumps(PR))
    bad["head"]["sha"] = "short"
    client, _ = _client({"GET /repos/org/repo/pulls/1": (bad, {})})
    with pytest.raises(AutomationError, match="no valid head SHA"):
        build_context(client, "org/repo", 1, {})


@pytest.mark.parametrize("repo", ["notavalidrepo", "org/repo/extra", "../org/repo", ""])
def test_build_context_validates_the_repository_name(repo):
    client, _ = _client({})
    with pytest.raises(AutomationError, match="Invalid target repository"):
        build_context(client, repo, 1, {})


def test_build_context_rejects_a_non_positive_pr_number():
    client, _ = _client({})
    with pytest.raises(AutomationError, match="positive integer"):
        build_context(client, "org/repo", 0, {})


def test_the_file_limit_is_configurable_and_reported():
    client, _ = _client(_context_routes(files=_files(4)))
    with pytest.raises(LimitExceeded, match="changed files"):
        build_context(client, "org/repo", 1, {"MAX_PR_FILES": "2"})


def test_the_file_limit_can_be_disabled():
    client, _ = _client(_context_routes(files=_files(4)))
    context = build_context(client, "org/repo", 1, {"MAX_PR_FILES": "none"})
    assert len(context["files"]) == 4


def test_a_malformed_limit_is_an_error():
    client, _ = _client(_context_routes())
    with pytest.raises(AutomationError, match="must be an integer"):
        build_context(client, "org/repo", 1, {"MAX_PR_FILES": "lots"})


def test_fetch_codeowners_returns_none_when_absent():
    import urllib.error

    class Missing:
        def __call__(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    client = GitHubClient("t", opener=Missing(), sleep=lambda _: None)
    assert fetch_codeowners(client, "org/repo", "b" * 40) is None
