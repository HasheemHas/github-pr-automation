"""CODEOWNERS-driven reviewer mapping and the three codeowners modes."""
from __future__ import annotations

from scripts.common.reviewers import DEFAULT_CODEOWNERS_MODE, resolve_reviewers

GROUPS = {
    "backend": {"paths": ["backend/**"], "users": ["backend-reviewer"], "teams": []},
    "frontend": {"paths": ["frontend/**"], "users": ["frontend-reviewer"], "teams": []},
}

OWNERS_ALL = "* @org/platform @owner-user\n"
OWNERS_BACKEND = "/backend/ @backend-owner @org/backend-team\n"


# -- ownership is always reported ------------------------------------------

def test_the_ownership_map_is_reported_even_when_deferring():
    # Previously coverage suppressed the request AND threw the mapping away; the
    # mapping is the useful half.
    result = resolve_reviewers(GROUPS, ["backend/app/a.py"], codeowners=OWNERS_ALL)
    assert result.deferred_to_codeowners is True
    assert result.ownership["users"] == ["owner-user"]
    assert result.ownership["teams"] == ["platform"]
    assert result.ownership["covers_everything"] is True
    assert result.ownership["per_owner"]


def test_the_deferral_note_names_the_owners():
    result = resolve_reviewers(GROUPS, ["backend/app/a.py"], codeowners=OWNERS_ALL)
    assert "@owner-user" in result.notes[0]


def test_suggestions_are_populated_from_codeowners():
    result = resolve_reviewers(GROUPS, ["backend/app/a.py"], codeowners=OWNERS_BACKEND)
    assert result.suggested_users == ["backend-owner"]
    assert result.suggested_teams == ["backend-team"]


def test_partial_coverage_still_uses_the_central_groups():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py", "frontend/src/b.tsx"], codeowners=OWNERS_BACKEND,
    )
    assert result.deferred_to_codeowners is False
    assert result.users == ["backend-reviewer", "frontend-reviewer"]
    assert result.suggested_users == ["backend-owner"]


def test_uncovered_paths_are_reported_with_a_note():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py", "docs/readme.md"], codeowners=OWNERS_BACKEND,
    )
    assert result.unowned_paths == ["docs/readme.md"]
    assert any("not covered by CODEOWNERS" in note for note in result.notes)


# -- modes -----------------------------------------------------------------

def test_defer_is_the_default_and_does_not_request_the_owners():
    result = resolve_reviewers(GROUPS, ["backend/app/a.py"], codeowners=OWNERS_ALL)
    assert DEFAULT_CODEOWNERS_MODE == "defer"
    assert result.users == [] and result.teams == []


def test_request_mode_adds_the_owners_to_the_request():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py"], codeowners=OWNERS_BACKEND,
        codeowners_mode="request",
    )
    assert "backend-owner" in result.users
    assert "backend-team" in result.teams
    assert any("requested explicitly" in note for note in result.notes)


def test_request_mode_does_not_defer_even_with_full_coverage():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py"], codeowners=OWNERS_ALL, codeowners_mode="request",
    )
    assert result.deferred_to_codeowners is False
    assert "owner-user" in result.users


def test_ignore_mode_reads_no_codeowners_at_all():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py"], codeowners=OWNERS_ALL, codeowners_mode="ignore",
    )
    assert result.ownership == {}
    assert result.suggested_users == []
    assert result.users == ["backend-reviewer"]


def test_an_unknown_mode_falls_back_to_defer_with_a_note():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py"], codeowners=OWNERS_ALL, codeowners_mode="nonsense",
    )
    assert result.deferred_to_codeowners is True
    assert any("Unknown reviewers.codeowners mode" in note for note in result.notes)


# -- the existing guarantees still hold ------------------------------------

def test_the_author_is_never_suggested_or_requested():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py"], author="backend-owner",
        codeowners=OWNERS_BACKEND, codeowners_mode="request",
    )
    assert "backend-owner" not in result.users
    assert result.suggested_users == []


def test_an_already_requested_owner_is_not_requested_again():
    result = resolve_reviewers(
        GROUPS, ["backend/app/a.py"], existing_users=["backend-owner"],
        codeowners=OWNERS_BACKEND, codeowners_mode="request",
    )
    assert "backend-owner" not in result.users
    assert result.suggested_users == []


def test_an_owner_matched_by_both_sources_is_requested_once():
    groups = {"backend": {"paths": ["backend/**"], "users": ["backend-owner"], "teams": []}}
    result = resolve_reviewers(
        groups, ["backend/app/a.py"], codeowners=OWNERS_BACKEND,
        codeowners_mode="request",
    )
    assert result.users.count("backend-owner") == 1


def test_no_codeowners_file_leaves_ownership_empty_and_groups_working():
    result = resolve_reviewers(GROUPS, ["backend/app/a.py"], codeowners=None)
    assert result.ownership == {}
    assert result.users == ["backend-reviewer"]


def test_matched_groups_with_only_owner_suggestions_does_not_claim_no_reviewers():
    groups = {"backend": {"paths": ["backend/**"], "users": [], "teams": []}}
    result = resolve_reviewers(
        groups, ["backend/app/a.py", "docs/x.md"], codeowners=OWNERS_BACKEND,
    )
    assert result.has_suggestions
    assert not any("no assignable users or teams" in note for note in result.notes)
