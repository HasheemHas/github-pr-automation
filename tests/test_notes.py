"""Release notes: commit classification, grouping, deployment actions."""
from __future__ import annotations

from scripts.insights.notes import generate, operator_actions, parse_commit, render_markdown


def _context(commits, *, files=(), title="", additions=0, deletions=0):
    return {
        "commits": list(commits),
        "files": list(files),
        "title": title,
        "additions": additions,
        "deletions": deletions,
    }


def _sections(result):
    return {section["key"]: section["entries"] for section in result["sections"]}


# -- commit classification -------------------------------------------------

def test_conventional_commits_are_parsed():
    parsed = parse_commit("feat(billing): add invoice export")
    assert parsed["section"] == "features"
    assert parsed["scope"] == "billing"
    assert parsed["description"] == "Add invoice export"
    assert parsed["conventional"] is True


def test_a_bang_marks_a_breaking_change():
    parsed = parse_commit("feat(api)!: drop the v1 endpoints")
    assert parsed["section"] == "breaking"
    assert parsed["breaking"] is True


def test_a_fix_commit_lands_in_fixed():
    assert parse_commit("fix: correct the rounding error")["section"] == "fixes"


def test_a_freeform_commit_is_classified_from_its_wording():
    assert parse_commit("Add support for CSV export")["section"] == "features"
    assert parse_commit("Fixed the broken login redirect")["section"] == "fixes"
    assert parse_commit("Refactor the invoice service")["section"] == "changed"
    assert parse_commit("Bump express to 5.0")["section"] == "dependencies"
    assert parse_commit("Speed up the dashboard query")["section"] == "performance"


def test_an_unclassifiable_commit_lands_in_changed():
    assert parse_commit("Miscellaneous")["section"] == "changed"


def test_a_trailing_issue_reference_is_lifted_out_of_the_text():
    parsed = parse_commit("fix: correct rounding (#412)")
    assert parsed["description"] == "Correct rounding"
    assert parsed["refs"] == ["#412"]


def test_a_jira_key_is_recognised():
    assert parse_commit("feat: add export ABC-123")["refs"] == ["ABC-123"]


# -- grouping --------------------------------------------------------------

def test_commits_are_grouped_into_sections():
    result = generate(_context([
        "feat: add invoice export", "fix: correct rounding", "docs: update readme",
    ]))
    sections = _sections(result)
    assert sections["features"] == ["Add invoice export"]
    assert sections["fixes"] == ["Correct rounding"]
    assert sections["docs"] == ["Update readme"]


def test_merge_and_revert_commits_are_skipped():
    result = generate(_context([
        "Merge branch 'main' into feature", "feat: add export",
    ]))
    entries = [e for entries in _sections(result).values() for e in entries]
    assert entries == ["Add export"]


def test_duplicate_descriptions_collapse_to_one_line():
    result = generate(_context(["fix: correct rounding", "fix: Correct rounding"]))
    assert _sections(result)["fixes"] == ["Correct rounding"]


def test_the_scope_is_rendered_in_bold():
    result = generate(_context(["feat(billing): add export"]))
    assert _sections(result)["features"] == ["**billing:** Add export"]


def test_internal_commits_are_hidden_by_default_and_can_be_included():
    context = _context(["chore: tidy imports", "feat: add export"])
    assert "internal" not in _sections(generate(context))
    included = generate(context, config={"release_notes": {"include_internal": True}})
    assert "internal" in _sections(included)


def test_the_pr_title_is_used_when_there_are_no_commits():
    result = generate(_context([], title="feat: add invoice export"))
    assert _sections(result)["features"] == ["Add invoice export"]


def test_entries_per_section_are_capped():
    result = generate(
        _context([f"fix: thing {n}" for n in range(40)]),
        config={"release_notes": {"max_per_section": 5}},
    )
    section = next(s for s in result["sections"] if s["key"] == "fixes")
    assert len(section["entries"]) == 5
    assert section["dropped"] == 35


def test_the_commit_style_is_reported():
    assert generate(_context(["feat: a", "fix: b"]))["commit_style"] == "conventional"
    assert generate(_context(["Add a thing"]))["commit_style"] == "freeform"
    assert generate(_context(["feat: a", "Add b"]))["commit_style"] == "mixed"


# -- deployment actions ----------------------------------------------------

def test_operator_actions_are_derived_from_changed_paths():
    actions = {a["label"] for a in operator_actions([
        "app/migrations/0042_add.py", "package.json", "Dockerfile",
    ])}
    assert {"Database migration", "Dependency change", "Container image change"} <= actions


def test_migrations_from_the_migration_insight_become_a_deployment_action():
    result = generate(
        _context(["feat: add status"]),
        insights={"migrations": {"status": "ok", "data": {"migrations": [
            {"path": "db/migration/V12__add.sql", "framework": "flyway",
             "risky_operations": ["drop-table"]},
        ]}}},
    )
    action = next(a for a in result["operator_actions"] if a["label"] == "Database migration")
    assert "destructive" in action["action"]


def test_a_major_dependency_bump_becomes_a_deployment_action():
    result = generate(
        _context(["chore: bump deps"]),
        insights={"dependencies": {"status": "ok", "data": {"totals": {"major_bumps": 2}}}},
    )
    assert any(a["label"] == "Major dependency upgrade" for a in result["operator_actions"])


# -- breaking changes ------------------------------------------------------

def test_an_api_insight_breaking_change_becomes_a_breaking_note():
    result = generate(
        _context(["feat: rework endpoints"]),
        insights={"api": {"status": "ok", "data": {
            "breaking_change": True,
            "routes": {"removed": ["GET /v1/invoices"]},
            "symbols": {"removed": []},
        }}},
    )
    assert result["breaking"] is True
    assert any("/v1/invoices" in entry for entry in _sections(result)["breaking"])


def test_a_breaking_change_label_adds_a_breaking_section():
    result = generate(_context(["feat: add export"]), labels=["breaking-change"])
    assert "breaking" in _sections(result)


# -- rendering -------------------------------------------------------------

def test_the_markdown_has_headings_bullets_and_a_stats_footer():
    result = generate(_context(
        ["feat: add export", "fix: correct rounding"],
        files=["app/a.py"], title="Add invoice export", additions=40, deletions=5,
    ))
    markdown = result["markdown"]
    assert "### Added" in markdown
    assert "- Add export" in markdown
    assert "+40/-5" in markdown


def test_deployment_notes_appear_in_the_markdown():
    result = generate(_context(["feat: add status"], files=["db/migrate/20240102_add.rb"]))
    assert "### Deployment Notes" in result["markdown"]


def test_references_appear_when_enabled_and_not_when_disabled():
    context = _context(["fix: correct rounding (#412)"])
    assert "#412" in generate(context)["markdown"]
    without = generate(context, config={"release_notes": {"include_issue_refs": False}})
    assert "### References" not in without["markdown"]


def test_render_markdown_of_nothing_is_empty():
    assert render_markdown([], []) == ""


def test_generation_can_be_disabled():
    result = generate(_context(["feat: a"]), config={"release_notes": {"enabled": False}})
    assert result["status"] == "not_run"


def test_an_empty_pull_request_does_not_crash():
    result = generate(_context([]))
    assert result["status"] == "pass"
    assert result["sections"] == []


def test_the_note_says_no_model_was_used():
    assert "No model was used" in generate(_context(["feat: a"]))["note"]
