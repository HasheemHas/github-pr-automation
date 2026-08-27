"""The managed PR description block."""
from __future__ import annotations

import pytest

from scripts.github.body import (
    DEFAULT_SECTIONS,
    FOOTER,
    SUMMARY_END,
    SUMMARY_START,
    render_block,
    upsert,
    valid_headings,
    valid_sections,
)

FULL = {
    "title": "feat(auth): add refresh-token validation",
    "summary": "Adds refresh-token validation.",
    "description": "Refresh tokens were accepted past their expiry.\n\nThis validates "
                   "`exp` before issuing a new access token.",
    "change_type": "feature",
    "risk": "high",
    "risk_reason": "touches the authentication path for every tenant",
    "breaking_change": True,
    "breaking_change_details": "Clients holding expired refresh tokens must re-authenticate.",
    "components": ["backend/auth", "ToastContext"],
    "changes": ["Validate exp before reissue", "Return 401 on expired refresh"],
    "release_notes": ["Fixed refresh tokens being accepted after expiry"],
    "recommended_testing": ["Log in, wait for expiry, attempt refresh"],
    "review_focus": ["backend/app/auth/jwt.py:74"],
    "migration_notes": ["Set AUTH_REFRESH_TTL in the environment"],
    "rollback": "Revert the commit; no data migration was applied.",
    "related_issues": ["#412"],
}

CONTEXT = {"files": ["backend/app/auth/jwt.py", "admin/src/x.jsx"],
           "additions": 42, "deletions": 7, "commits": ["a", "b", "c"]}


# -- no AI banner ----------------------------------------------------------

def test_the_block_does_not_open_with_an_ai_summary_banner():
    block = render_block(FULL, CONTEXT)
    assert "AI PR Summary" not in block
    # The first heading after the marker is the description itself.
    first_line = block.split(SUMMARY_START, 1)[1].strip().splitlines()[0]
    assert first_line == "## Description"


def test_the_block_is_marker_delimited():
    block = render_block(FULL, CONTEXT)
    assert block.startswith(SUMMARY_START)
    assert block.endswith(SUMMARY_END)


def test_a_generated_footer_is_present():
    assert FOOTER in render_block(FULL, CONTEXT)


# -- sections --------------------------------------------------------------

@pytest.mark.parametrize(
    "heading",
    [
        "## Description",
        "## Change Type",
        "## Risk",
        "## Components",
        "## Changes",
        "## Release Notes",
        "## Breaking Changes",
        "## Migration Notes",
        "## Recommended Testing",
        "## Review Focus",
        "## Rollback",
        "## Related Issues",
        "## Change Stats",
    ],
)
def test_every_section_renders_when_it_has_content(heading):
    assert heading in render_block(FULL, CONTEXT)


def test_the_description_carries_the_long_form_text():
    block = render_block(FULL, CONTEXT)
    assert "Refresh tokens were accepted past their expiry." in block
    assert "validates `exp` before issuing" in block


def test_the_description_falls_back_to_the_summary():
    block = render_block({"summary": "Short and sweet.", "change_type": "chore"})
    assert "## Description\n\nShort and sweet." in block


def test_risk_shows_the_level_reason_and_an_icon():
    block = render_block(FULL, CONTEXT)
    assert "🔴 **high** — touches the authentication path" in block


def test_risk_without_a_reason_still_renders():
    block = render_block({"summary": "s", "risk": "low"})
    assert "🟢 **low**" in block
    assert "—" not in block.split("## Risk")[1].splitlines()[2]


def test_release_notes_render_as_changelog_bullets():
    block = render_block(FULL, CONTEXT)
    notes = block.split("## Release Notes")[1].split("##")[0]
    assert "- Fixed refresh tokens being accepted after expiry" in notes


def test_components_are_code_formatted():
    assert "`backend/auth`, `ToastContext`" in render_block(FULL, CONTEXT)


def test_change_stats_come_from_the_pr_not_the_model():
    block = render_block({"summary": "s"}, CONTEXT)
    assert "**Files changed:** 2" in block
    assert "**Lines:** +42 / -7" in block
    assert "**Commits:** 3" in block


def test_change_stats_are_omitted_without_context():
    assert "## Change Stats" not in render_block({"summary": "s"})


# -- empty sections --------------------------------------------------------

def test_empty_sections_are_omitted_rather_than_padded():
    block = render_block({"summary": "One-line change.", "change_type": "chore",
                          "risk": "low"})
    for absent in ("## Components", "## Changes", "## Release Notes",
                   "## Migration Notes", "## Recommended Testing", "## Review Focus",
                   "## Rollback", "## Related Issues"):
        assert absent not in block
    assert "N/A" not in block and "None." in block  # only Breaking Changes says None


def test_breaking_changes_is_always_stated():
    # "No breaking changes" is information a release manager needs.
    assert "## Breaking Changes\n\nNone." in render_block({"summary": "s"})


def test_a_breaking_change_is_flagged_with_its_details():
    block = render_block(FULL, CONTEXT)
    assert "⚠️ **Yes.** Clients holding expired refresh tokens" in block


def test_a_breaking_change_without_details_still_says_yes():
    assert "⚠️ **Yes.**" in render_block({"summary": "s", "breaking_change": True})


def test_a_trivial_pr_produces_a_short_block():
    block = render_block({"summary": "Added a trailing comment.", "change_type": "chore",
                          "risk": "low"}, {"files": ["a.jsx"], "additions": 1,
                                           "deletions": 0, "commits": ["x"]})
    headings = [line for line in block.splitlines() if line.startswith("## ")]
    assert headings == ["## Description", "## Change Type", "## Risk",
                        "## Breaking Changes", "## Change Stats"]


# -- section configuration -------------------------------------------------

def test_sections_can_be_reordered_and_trimmed():
    block = render_block(FULL, CONTEXT, sections=["release_notes", "description"])
    assert block.index("## Release Notes") < block.index("## Description")
    assert "## Risk" not in block
    assert "## Change Stats" not in block


def test_an_unknown_section_name_is_ignored():
    block = render_block(FULL, CONTEXT, sections=["description", "not_a_section"])
    assert "## Description" in block
    assert "not_a_section" not in block


def test_valid_sections_filters_unknown_names():
    assert valid_sections(["description", "bogus", "risk"]) == ["description", "risk"]


def test_valid_sections_returns_none_for_nothing_usable():
    assert valid_sections(None) is None
    assert valid_sections("description") is None
    assert valid_sections([]) is None
    assert valid_sections(["bogus", "also-bogus"]) is None


def test_the_at_a_glance_template_renders_the_compact_pr_shape():
    block = render_block(
        FULL,
        CONTEXT,
        sections=["at_glance", "details", "test_plan", "type", "user_impact", "release_notes"],
        headings={
            "at_glance": "At a Glance",
            "details": "Details",
            "test_plan": "Test Plan",
            "type": "Type",
            "user_impact": "User Impact",
        },
    )
    assert "## At a Glance" in block
    assert "**Risk:** High" in block
    assert "## Details" in block
    assert "1. Log in, wait for expiry, attempt refresh" in block
    assert "## Type\n\nFeature" in block
    assert "## User Impact" not in block


def test_configured_headings_override_the_visible_template_names():
    block = render_block(
        FULL,
        CONTEXT,
        sections=["at_glance"],
        headings={"at_glance": "Overview"},
    )
    assert "## Overview" in block
    assert "## At a Glance" not in block


def test_valid_headings_filters_unknown_names():
    assert valid_headings({"at_glance": "Overview", "bogus": "Nope"}) == {
        "at_glance": "Overview"
    }
    assert valid_headings(None) is None
    assert valid_headings([]) is None


def test_the_default_order_leads_with_the_description():
    assert DEFAULT_SECTIONS[0] == "description"


def test_the_shipped_config_lists_only_valid_sections():
    from scripts.common.config import load_repositories

    for name, entry in load_repositories("config/repositories.yml").items():
        declared = (entry.get("metadata") or {}).get("sections")
        if declared:
            assert valid_sections(declared) == list(declared), name


# -- upsert ----------------------------------------------------------------

def test_author_text_outside_the_block_is_preserved():
    body = "My own notes.\n\nSee the ticket."
    updated = upsert(body, FULL, CONTEXT)
    assert updated.startswith("My own notes.")
    assert "See the ticket." in updated
    assert SUMMARY_START in updated


def test_regenerating_replaces_only_the_block():
    first = upsert("Author text.", {"summary": "First pass."}, CONTEXT)
    second = upsert(first, {"summary": "Second pass."}, CONTEXT)
    assert second.count(SUMMARY_START) == 1
    assert "Second pass." in second
    assert "First pass." not in second
    assert second.startswith("Author text.")


def test_an_empty_body_becomes_just_the_block():
    updated = upsert("", FULL, CONTEXT)
    assert updated.startswith(SUMMARY_START)


def test_a_body_that_is_only_the_block_does_not_accumulate_blank_lines():
    once = upsert("", {"summary": "s"}, CONTEXT)
    twice = upsert(once, {"summary": "s"}, CONTEXT)
    assert once == twice


def test_the_report_module_still_exposes_upsert_summary():
    from scripts.github.report import upsert_summary

    updated = upsert_summary("", "Fallback text.", {"change_type": "chore"})
    assert "## Description\n\nFallback text." in updated


def test_upsert_summary_prefers_an_explicit_description():
    from scripts.github.report import upsert_summary

    updated = upsert_summary("", "short", {"description": "The long version."})
    assert "The long version." in updated
    assert "## Description\n\nshort" not in updated
