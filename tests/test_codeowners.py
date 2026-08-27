"""CODEOWNERS parsing and ownership resolution, following GitHub's own rules."""
from __future__ import annotations

from scripts.common.codeowners import (
    classify,
    covers,
    owners_for,
    parse,
    resolve,
    suggestions,
    summarise,
)

FILE = """\
# Default owners for everything
*                       @org/platform

# Backend
/backend/               @backend-lead @org/backend

# Frontend, at any depth
frontend/               @frontend-lead

# One specific file
/backend/app/billing.py @billing-owner

# Deliberately ownerless: excludes generated code from the default rule above
/backend/generated/
"""


def test_parse_keeps_patterns_and_owners_and_drops_comments():
    rules = parse(FILE)
    patterns = [rule.pattern for rule in rules]
    assert patterns == [
        "*", "/backend/", "frontend/", "/backend/app/billing.py", "/backend/generated/",
    ]
    assert rules[1].owners == ("@backend-lead", "@org/backend")


def test_the_last_matching_rule_wins_outright():
    # billing.py matches *, /backend/ AND its own rule. GitHub takes the last one
    # and does not merge, so the backend owners must not appear.
    assert owners_for(parse(FILE), "backend/app/billing.py") == ("@billing-owner",)


def test_an_earlier_broad_rule_applies_where_nothing_more_specific_matches():
    assert owners_for(parse(FILE), "README.md") == ("@org/platform",)


def test_a_directory_rule_covers_everything_beneath_it():
    assert owners_for(parse(FILE), "backend/app/models.py") == (
        "@backend-lead", "@org/backend",
    )


def test_an_unanchored_directory_rule_matches_at_any_depth():
    assert owners_for(parse(FILE), "packages/frontend/src/App.tsx") == ("@frontend-lead",)
    assert owners_for(parse(FILE), "frontend/src/App.tsx") == ("@frontend-lead",)


def test_a_matching_ownerless_rule_clears_ownership():
    # This is how CODEOWNERS excludes a subtree, so it must not fall back to *.
    assert owners_for(parse(FILE), "backend/generated/client.py") == ()


def test_resolve_splits_users_teams_and_emails():
    text = "* @a-user @org/a-team owner@example.com\n"
    ownership = resolve(text, ["x.py"])
    assert ownership.users == ["a-user"]
    assert ownership.teams == ["a-team"]
    assert ownership.emails == ["owner@example.com"]


def test_resolve_records_unowned_paths():
    ownership = resolve("/backend/ @backend-lead\n", ["backend/a.py", "frontend/b.tsx"])
    assert ownership.by_path == {"backend/a.py": ["backend-lead"]}
    assert ownership.unowned == ["frontend/b.tsx"]
    assert not ownership.covers_everything


def test_covers_everything_requires_every_path_to_be_owned():
    assert covers("* @org/all\n", ["a.py", "b/c.py"])
    assert not covers("/backend/ @x\n", ["backend/a.py", "docs/b.md"])
    # An empty change set is never "covered": there is nothing to defer.
    assert not covers("* @org/all\n", [])


def test_a_file_with_no_rules_leaves_everything_unowned():
    ownership = resolve("# only comments\n", ["a.py"])
    assert ownership.unowned == ["a.py"]
    assert ownership.empty


def test_classify_recognises_the_three_owner_forms():
    assert classify("@someone") == ("user", "someone")
    assert classify("@org/team-slug") == ("team", "team-slug")
    assert classify("dev@example.com") == ("email", "dev@example.com")
    assert classify("  ") == ("", "")


def test_section_headings_are_not_treated_as_patterns():
    rules = parse("[Backend]\n/backend/ @x\n")
    assert [rule.pattern for rule in rules] == ["/backend/"]


def test_suggestions_never_include_the_author_or_an_existing_reviewer():
    ownership = resolve("* @author @other @org/team\n", ["a.py"])
    result = suggestions(
        ownership, author="@Author", existing_users=["other"], existing_teams=[],
    )
    assert result["users"] == []
    assert result["teams"] == ["team"]


def test_summarise_counts_files_per_owner_most_first():
    text = "* @wide\n/backend/ @backend-lead\n"
    ownership = resolve(text, ["backend/a.py", "backend/b.py", "docs/c.md"])
    assert summarise(ownership) == ["@backend-lead — 2 file(s)", "@wide — 1 file(s)"]


def test_to_dict_caps_the_per_path_map():
    ownership = resolve("* @x\n", [f"file{n}.py" for n in range(400)])
    assert len(ownership.to_dict()["by_path"]) == 200
    assert ownership.covers_everything
