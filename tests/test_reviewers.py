"""Reviewer resolution: author exclusion, de-duplication, CODEOWNERS deference."""
from __future__ import annotations

from scripts.common.reviewers import codeowners_covers, resolve_reviewers

GROUPS = {
    "backend": {"paths": ["backend/**"], "users": ["backend-reviewer"], "teams": []},
    "frontend": {"paths": ["frontend/**", "admin/**"], "users": ["frontend-reviewer"], "teams": []},
    "devops": {
        "paths": ["terraform/**", "helm/**", ".github/workflows/**"],
        "users": [],
        "teams": ["devops-team"],
    },
    "security": {"paths": ["**/auth/**", "security/**"], "users": [], "teams": ["security-team"]},
}


def test_matching_group_requests_its_reviewers():
    result = resolve_reviewers(GROUPS, ["backend/app/main.py"])
    assert result.users == ["backend-reviewer"]
    assert result.matched_groups == ["backend"]


def test_multiple_groups_combine():
    result = resolve_reviewers(
        GROUPS, ["backend/app/main.py", "frontend/src/App.tsx", "terraform/main.tf"]
    )
    assert set(result.users) == {"backend-reviewer", "frontend-reviewer"}
    assert result.teams == ["devops-team"]


def test_no_match_requests_nobody_and_says_why():
    result = resolve_reviewers(GROUPS, ["README.md"])
    assert result.empty
    assert result.notes == ["No reviewer group matched the changed files."]


# -- author exclusion ------------------------------------------------------

def test_the_pr_author_is_never_requested():
    result = resolve_reviewers(GROUPS, ["backend/app/main.py"], author="backend-reviewer")
    assert result.users == []
    assert "backend" in result.matched_groups


def test_author_exclusion_is_case_insensitive_and_ignores_an_at_prefix():
    result = resolve_reviewers(GROUPS, ["backend/app/main.py"], author="@Backend-Reviewer")
    assert result.users == []


def test_the_author_is_excluded_but_others_are_kept():
    result = resolve_reviewers(
        GROUPS, ["backend/app/main.py", "frontend/src/a.tsx"], author="backend-reviewer"
    )
    assert result.users == ["frontend-reviewer"]


# -- de-duplication --------------------------------------------------------

def test_a_reviewer_matched_by_two_groups_is_requested_once():
    groups = {
        "a": {"paths": ["backend/**"], "users": ["shared"], "teams": ["shared-team"]},
        "b": {"paths": ["**/*.py"], "users": ["shared", "@shared"], "teams": ["shared-team"]},
    }
    result = resolve_reviewers(groups, ["backend/app/main.py"])
    assert result.users == ["shared"]
    assert result.teams == ["shared-team"]


def test_already_requested_reviewers_are_not_requested_again():
    result = resolve_reviewers(
        GROUPS,
        ["backend/app/main.py", "terraform/main.tf"],
        existing_users=["backend-reviewer"],
        existing_teams=["devops-team"],
    )
    assert result.empty


def test_existing_reviewers_are_preserved_by_only_ever_adding():
    result = resolve_reviewers(
        GROUPS,
        ["backend/app/main.py", "frontend/src/a.tsx"],
        existing_users=["Backend-Reviewer"],
    )
    # Nothing is removed; only the genuinely new reviewer is returned.
    assert result.users == ["frontend-reviewer"]


# -- missing users and teams ----------------------------------------------

def test_an_empty_group_reports_rather_than_failing():
    groups = {"backend": {"paths": ["backend/**"], "users": [], "teams": []}}
    result = resolve_reviewers(groups, ["backend/app/main.py"])
    assert result.empty
    assert "no assignable users or teams" in result.notes[0]


def test_a_malformed_group_is_skipped_with_a_note():
    groups = {"broken": "not-a-mapping", "backend": GROUPS["backend"]}
    result = resolve_reviewers(groups, ["backend/app/main.py"])
    assert result.users == ["backend-reviewer"]
    assert any("not a mapping" in note for note in result.notes)


def test_a_group_without_paths_is_skipped():
    groups = {"nopaths": {"users": ["x"]}}
    result = resolve_reviewers(groups, ["backend/app/main.py"])
    assert result.empty
    assert any("declares no paths" in note for note in result.notes)


def test_non_string_entries_are_dropped():
    groups = {"backend": {"paths": ["backend/**"], "users": ["ok", 42, None, ""], "teams": []}}
    result = resolve_reviewers(groups, ["backend/app/main.py"])
    assert result.users == ["ok"]


# -- CODEOWNERS ------------------------------------------------------------

def test_codeowners_covering_everything_defers_entirely():
    result = resolve_reviewers(
        GROUPS, ["backend/app/main.py"], codeowners="* @org/everyone\n"
    )
    assert result.deferred_to_codeowners
    assert result.empty
    assert "CODEOWNERS" in result.notes[0]


def test_codeowners_covering_only_some_files_does_not_defer():
    result = resolve_reviewers(
        GROUPS,
        ["backend/app/main.py", "frontend/src/a.tsx"],
        codeowners="/backend/ @org/backend\n",
    )
    assert not result.deferred_to_codeowners
    assert "frontend-reviewer" in result.users


def test_codeowners_covers_recognises_directory_and_glob_rules():
    text = "/backend/ @org/backend\n*.tsx @org/web\n"
    assert codeowners_covers(text, ["backend/app/main.py", "frontend/src/a.tsx"])
    assert not codeowners_covers(text, ["docs/readme.md"])


def test_codeowners_comments_and_ownerless_rules_are_ignored():
    text = "# comment\n/backend/\n/docs/ @org/docs\n"
    # /backend/ has no owners, so it assigns nobody and cannot cover the file.
    assert not codeowners_covers(text, ["backend/app/main.py"])
    assert codeowners_covers(text, ["docs/readme.md"])


def test_an_empty_codeowners_file_does_not_defer():
    assert not codeowners_covers("", ["backend/a.py"])
    assert not codeowners_covers("# only a comment\n", ["backend/a.py"])


def test_codeowners_never_defers_on_an_empty_change_set():
    assert not codeowners_covers("* @org/everyone", [])


def test_to_dict_shape():
    payload = resolve_reviewers(GROUPS, ["backend/app/main.py"]).to_dict()
    assert set(payload) == {
        "users", "teams", "matched_groups", "deferred_to_codeowners", "notes",
        "ownership", "suggested_users", "suggested_teams", "unowned_paths",
    }
