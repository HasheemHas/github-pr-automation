"""Repository-wide test discovery, and the batched review path in analyse()."""
from __future__ import annotations

import pytest

from scripts.ai.analyze import analyse, review_in_batches
from scripts.common.dispatch import plan as build_plan
from scripts.common.errors import AIError
from scripts.github.api import GitHubClient
from scripts.github.get_pr import TEST_PATTERNS, fetch_test_inventory
from tests.conftest import FakeAIClient, FakeOpener

TREE = {
    "truncated": False,
    "tree": [
        {"path": "backend/app/auth.py", "type": "blob"},
        {"path": "backend/tests/test_login.py", "type": "blob"},
        {"path": "backend/tests/test_auth.py", "type": "blob"},
        {"path": "backend/tests", "type": "tree"},
        {"path": "frontend/src/App.tsx", "type": "blob"},
        {"path": "frontend/src/App.test.tsx", "type": "blob"},
        {"path": "admin/src/login.spec.ts", "type": "blob"},
        {"path": "conftest.py", "type": "blob"},
        {"path": "README.md", "type": "blob"},
    ],
}


def _client(routes):
    return GitHubClient("token", opener=FakeOpener(routes), sleep=lambda _: None)


HEAD = "a" * 40


def test_the_inventory_finds_tests_the_pr_never_touched():
    # The exact reported bug: a PR editing only backend/app/auth.py used to be
    # told the repository had no tests.
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (TREE, {})})
    inventory = fetch_test_inventory(client, "org/repo", HEAD)
    assert inventory["tests"] == [
        "admin/src/login.spec.ts",
        "backend/tests/test_auth.py",
        "backend/tests/test_login.py",
        "conftest.py",
        "frontend/src/App.test.tsx",
    ]
    assert inventory["test_count"] == 5
    assert inventory["truncated"] is False


def test_directories_are_not_counted_as_test_files():
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (TREE, {})})
    assert "backend/tests" not in fetch_test_inventory(client, "org/repo", HEAD)["tests"]


def test_non_test_files_are_excluded():
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (TREE, {})})
    tests = fetch_test_inventory(client, "org/repo", HEAD)["tests"]
    assert "backend/app/auth.py" not in tests
    assert "README.md" not in tests


def test_the_full_file_count_is_recorded():
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (TREE, {})})
    assert fetch_test_inventory(client, "org/repo", HEAD)["total_files"] == 8


def test_a_truncated_tree_is_flagged_rather_than_trusted():
    tree = {**TREE, "truncated": True}
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (tree, {})})
    inventory = fetch_test_inventory(client, "org/repo", HEAD)
    assert inventory["truncated"] is True
    assert inventory["tree_truncated"] is True


def test_capping_the_list_also_flags_truncation():
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (TREE, {})})
    inventory = fetch_test_inventory(client, "org/repo", HEAD, limit=2)
    assert len(inventory["tests"]) == 2
    assert inventory["truncated"] is True
    assert inventory["test_count"] == 5


def test_a_repository_with_no_tests_reports_an_empty_inventory_not_a_failure():
    tree = {"truncated": False, "tree": [{"path": "app.py", "type": "blob"}]}
    client = _client({f"GET /repos/org/repo/git/trees/{HEAD}?recursive=1": (tree, {})})
    inventory = fetch_test_inventory(client, "org/repo", HEAD)
    assert inventory["tests"] == []
    assert "error" not in inventory


def test_a_tree_read_failure_is_reported_not_silently_empty():
    import urllib.error

    class Opener:
        def __call__(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)

    client = GitHubClient("t", opener=Opener(), sleep=lambda _: None)
    inventory = fetch_test_inventory(client, "org/repo", HEAD)
    assert inventory["tests"] == []
    assert "Could not read the repository tree" in inventory["error"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "test/test_a.py",
        "backend/tests/test_a.py",
        "__tests__/a.js",
        "src/__tests__/a.js",
        "src/a_test.py",
        "src/test_a.py",
        "pkg/a_test.go",
        "src/a.test.js",
        "src/a.test.tsx",
        "src/a.spec.ts",
        "spec/a_spec.rb",
        "conftest.py",
        "backend/conftest.py",
    ],
)
def test_the_conventional_test_layouts_are_all_matched(path):
    from scripts.common.globs import any_match

    assert any_match(TEST_PATTERNS, [path])


@pytest.mark.parametrize("path", ["backend/app/auth.py", "README.md", "src/latest.py",
                                  "src/protest.py", "contest/a.py"])
def test_non_test_paths_are_not_matched(path):
    from scripts.common.globs import any_match

    assert not any_match(TEST_PATTERNS, [path])


# -- the inventory reaches the prompt --------------------------------------

CONTEXT = {
    "repository": "org/repo",
    "number": 7,
    "title": "Add refresh-token validation",
    "body": "",
    "author": "dev",
    "head_sha": HEAD,
    "files": ["backend/app/auth.py"],
    "commits": ["Add validation"],
    "repository_tests": ["backend/tests/test_auth.py", "backend/tests/test_login.py"],
    "test_inventory": {"truncated": False, "test_count": 2},
}


def test_the_tests_prompt_receives_the_whole_repository_inventory():
    fake = FakeAIClient([{"changed": [], "existing_tests": [], "gaps": []}])
    analyse(build_plan("tests"), CONTEXT, "diff", client=fake)
    prompt = fake.completions.calls[0]["messages"][1]["content"]
    assert "backend/tests/test_auth.py" in prompt
    assert "backend/tests/test_login.py" in prompt
    assert "complete inventory of test files" in prompt


def test_the_prompt_says_absence_is_real_when_the_repo_has_no_tests():
    context = {**CONTEXT, "repository_tests": [], "test_inventory": {"truncated": False}}
    fake = FakeAIClient([{"changed": [], "existing_tests": [], "gaps": []}])
    analyse(build_plan("tests"), context, "diff", client=fake)
    prompt = fake.completions.calls[0]["messages"][1]["content"]
    assert "Absence here is a real finding" in prompt


def test_a_truncated_inventory_tells_the_model_not_to_conclude_absence():
    context = {**CONTEXT, "test_inventory": {"truncated": True}}
    fake = FakeAIClient([{"changed": [], "existing_tests": [], "gaps": []}])
    analyse(build_plan("tests"), context, "diff", client=fake)
    prompt = fake.completions.calls[0]["messages"][1]["content"]
    assert "treat absence from the list as unknown" in prompt


def test_the_inventory_source_is_recorded_in_the_analysis():
    fake = FakeAIClient([{"changed": [], "existing_tests": [], "gaps": []}])
    result = analyse(build_plan("tests"), CONTEXT, "diff", client=fake)
    assert result["test_inventory"] == {
        "count": 2, "truncated": False, "source": "repository tree at head"
    }


# -- batched review --------------------------------------------------------

def _diff(paths, lines=200):
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,1 +1,{lines} @@\n"
        + "".join(f"+line {i} in {p}\n" for i in range(lines))
        for p in paths
    )


def test_a_large_diff_is_reviewed_across_several_requests():
    paths = [f"backend/app/f{i}.py" for i in range(20)]
    # Each file is larger than the batch budget, so files are also split by hunk.
    fake = FakeAIClient([{"findings": []}] * 200)
    result = review_in_batches(
        {"files": paths}, _diff(paths), security_only=False, client=fake,
        env={"AI_REVIEW_BATCH_CHARS": "3000", "AI_REVIEW_BATCH_FILES": "2",
             "AI_REVIEW_MAX_BATCHES": "100"},
    )
    assert result["batches"] > 1
    assert len(fake.completions.calls) == result["batches"]
    assert result["complete"] is True
    assert len(result["reviewed_paths"]) == 20


def test_findings_from_every_batch_are_merged():
    paths = ["a.py", "b.py", "c.py", "d.py"]
    fake = FakeAIClient(
        [
            {"findings": [{"severity": "high", "title": "one", "path": "a.py", "line": 2}]},
            {"findings": [{"severity": "low", "title": "two", "path": "c.py", "line": 3}]},
        ]
    )
    result = review_in_batches(
        {"files": paths}, _diff(paths, lines=50), security_only=False, client=fake,
        env={"AI_REVIEW_BATCH_CHARS": "1500", "AI_REVIEW_BATCH_FILES": "2",
             "AI_REVIEW_MAX_BATCHES": "50"},
    )
    assert [f["title"] for f in result["findings"]] == ["one", "two"]


def test_exceeding_the_batch_cap_marks_the_review_incomplete():
    paths = [f"f{i}.py" for i in range(30)]
    fake = FakeAIClient([{"findings": []}] * 30)
    result = review_in_batches(
        {"files": paths}, _diff(paths, lines=100), security_only=False, client=fake,
        env={"AI_REVIEW_BATCH_CHARS": "1500", "AI_REVIEW_BATCH_FILES": "1",
             "AI_REVIEW_MAX_BATCHES": "3"},
    )
    assert result["complete"] is False
    assert result["unreviewed_paths"]
    assert "were not reviewed" in result["note"]


def test_one_failed_batch_keeps_the_others_but_marks_the_review_incomplete():
    class Boom(Exception):
        status_code = 500

    paths = ["a.py", "b.py", "c.py", "d.py"]
    fake = FakeAIClient([{"findings": []}, Boom()])
    result = review_in_batches(
        {"files": paths}, _diff(paths, lines=50), security_only=False, client=fake,
        env={"AI_REVIEW_BATCH_CHARS": "1500", "AI_REVIEW_BATCH_FILES": "2",
             "AI_REVIEW_MAX_BATCHES": "50"},
    )
    assert result["succeeded_batches"] == 1
    assert result["complete"] is False
    assert "review batch(es) failed" in result["note"]


def test_every_batch_failing_is_an_error_not_a_clean_review():
    class Boom(Exception):
        status_code = 500

    fake = FakeAIClient([Boom(), Boom(), Boom(), Boom()])
    with pytest.raises(AIError, match="Every review batch failed"):
        review_in_batches(
            {"files": ["a.py"]}, _diff(["a.py"]), security_only=False, client=fake
        )


def test_an_empty_diff_is_a_complete_review_of_nothing():
    result = review_in_batches({"files": []}, "", security_only=False, client=None)
    assert result == {
        "findings": [], "dropped": 0, "batches": 0, "complete": True,
        "reviewed_paths": [], "unreviewed_paths": [], "note": "The diff was empty.",
    }


def test_each_batch_prompt_names_its_own_scope():
    paths = ["a.py", "b.py", "c.py", "d.py"]
    fake = FakeAIClient([{"findings": []}] * 4)
    review_in_batches(
        {"files": paths}, _diff(paths, lines=50), security_only=False, client=fake,
        env={"AI_REVIEW_BATCH_CHARS": "1500", "AI_REVIEW_BATCH_FILES": "1",
             "AI_REVIEW_MAX_BATCHES": "50"},
    )
    first = fake.completions.calls[0]["messages"][1]["content"]
    assert "batch 1 of 4" in first
    assert "Do not comment on" in first


def test_a_single_batch_prompt_has_no_batch_preamble():
    fake = FakeAIClient([{"findings": []}])
    review_in_batches(
        {"files": ["a.py"]}, _diff(["a.py"], lines=5), security_only=False, client=fake
    )
    assert "batch 1 of 1" not in fake.completions.calls[0]["messages"][1]["content"]


def test_the_analyse_review_section_uses_batching():
    paths = ["a.py", "b.py"]
    fake = FakeAIClient([{"findings": []}, {"findings": []}])
    result = analyse(
        build_plan("review"),
        {**CONTEXT, "files": paths},
        _diff(paths, lines=50),
        client=fake,
        env={"AI_REVIEW_BATCH_CHARS": "1500", "AI_REVIEW_BATCH_FILES": "1",
             "AI_REVIEW_MAX_BATCHES": "50"},
    )
    assert result["review"]["status"] == "ok"
    assert result["review"]["data"]["batches"] == 2
