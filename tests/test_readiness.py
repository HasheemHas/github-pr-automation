"""Merge readiness: which dimensions block, and how the verdict is reached."""
from __future__ import annotations

from scripts.common.readiness import (
    BLOCKED,
    NOT_RUN,
    READY,
    VERDICT_NOT_READY,
    VERDICT_READY,
    VERDICT_WARNINGS,
    WARN,
    assess,
    conflict_state,
    render_check_text,
    render_markdown,
    review_state,
)


def _sections(**statuses):
    names = {
        "lint": "Lint", "test": "Test", "build": "Build", "security": "Security",
        "secrets": "Secret scan", "coverage": "Changed-line coverage",
        "size": "PR size", "api": "API compatibility", "metadata": "Metadata",
    }
    return [
        {"name": names[key], "status": value, "detail": f"{key} detail"}
        for key, value in statuses.items()
    ]


def _dimension(readiness, key):
    return next(d for d in readiness.dimensions if d.key == key)


APPROVED = {"approvals": ["reviewer"], "mergeable": True, "mergeable_state": "clean"}


# -- the happy path --------------------------------------------------------

def test_everything_passing_with_an_approval_is_ready():
    readiness = assess(
        sections=_sections(lint="PASS", test="PASS", build="PASS", security="PASS",
                           secrets="PASS"),
        context=APPROVED,
    )
    assert readiness.verdict == VERDICT_READY
    assert readiness.ready is True
    assert readiness.blockers == []


def test_an_advisory_warning_makes_it_ready_with_warnings():
    readiness = assess(
        sections=_sections(lint="PASS", test="PASS", build="PASS", security="PASS",
                           secrets="PASS", size="WARN"),
        context=APPROVED,
    )
    assert readiness.verdict == VERDICT_WARNINGS
    assert readiness.ready is True
    assert any("PR size" in warning for warning in readiness.warnings)


# -- blocking --------------------------------------------------------------

def test_a_failing_blocking_check_makes_it_not_ready():
    readiness = assess(
        sections=_sections(lint="PASS", test="FAIL", build="PASS", security="PASS",
                           secrets="PASS"),
        context=APPROVED,
    )
    assert readiness.verdict == VERDICT_NOT_READY
    assert readiness.ready is False
    assert any("Test" in blocker for blocker in readiness.blockers)


def test_a_failing_non_blocking_check_is_only_a_warning():
    readiness = assess(
        sections=_sections(lint="PASS", test="PASS", build="PASS", security="PASS",
                           secrets="PASS", api="FAIL"),
        context=APPROVED,
    )
    # api is not in the default block_on list, so it advises rather than blocks.
    assert readiness.verdict == VERDICT_WARNINGS
    assert _dimension(readiness, "api").holds_the_merge is False


def test_a_blocking_check_that_never_ran_holds_the_merge():
    # A check that was required but skipped is not a pass.
    readiness = assess(
        sections=_sections(lint="NOT RUN", test="PASS", build="PASS", security="PASS",
                           secrets="PASS"),
        context=APPROVED,
    )
    assert readiness.verdict == VERDICT_NOT_READY
    assert any("did not run" in note for note in readiness.notes)


def test_an_unrequested_non_blocking_check_is_not_a_warning():
    # Listing every check nobody asked for would bury the ones that matter.
    readiness = assess(
        sections=_sections(lint="PASS", test="PASS", build="PASS", security="PASS",
                           secrets="PASS", api="NOT RUN", size="NOT RUN"),
        context=APPROVED,
    )
    assert readiness.verdict == VERDICT_READY
    assert readiness.warnings == []
    assert _dimension(readiness, "api").status == NOT_RUN


def test_block_on_is_configurable():
    sections = _sections(lint="FAIL", test="PASS", build="PASS", security="PASS",
                         secrets="PASS")
    strict = assess(sections=sections, context=APPROVED)
    lenient = assess(sections=sections, context=APPROVED,
                     config={"block_on": ["security", "secrets"]})
    assert strict.verdict == VERDICT_NOT_READY
    assert lenient.verdict == VERDICT_WARNINGS


def test_an_empty_block_on_list_makes_everything_advisory():
    readiness = assess(
        sections=_sections(lint="FAIL", test="FAIL"),
        context=APPROVED,
        config={"block_on": [], "require_approvals": 0},
    )
    assert readiness.blockers == []
    assert readiness.verdict == VERDICT_WARNINGS


def test_a_malformed_block_on_falls_back_to_the_defaults():
    readiness = assess(
        sections=_sections(test="FAIL"), context=APPROVED,
        config={"block_on": "everything"},
    )
    assert readiness.verdict == VERDICT_NOT_READY


# -- human review ----------------------------------------------------------

def test_changes_requested_blocks_regardless_of_approvals():
    dimension = review_state(
        {"approvals": ["a", "b"], "changes_requested": ["c"]}, required=1
    )
    assert dimension.status == BLOCKED
    assert "@c" in dimension.detail


def test_the_required_approval_count_is_honoured():
    assert review_state({"approvals": ["a"]}, required=1).status == READY
    assert review_state({"approvals": ["a"]}, required=2).status == BLOCKED


def test_zero_required_approvals_still_reports_the_state():
    with_approval = review_state({"approvals": ["a"]}, required=0)
    without = review_state({"approvals": []}, required=0)
    assert with_approval.status == READY
    assert without.status == WARN


def test_a_reviewer_who_commented_without_approving_is_named():
    dimension = review_state(
        {"approvals": [], "reviewed_by": ["commenter"]}, required=1
    )
    assert "without approving" in dimension.detail


def test_a_missing_approval_blocks_the_merge():
    readiness = assess(
        sections=_sections(lint="PASS", test="PASS", build="PASS", security="PASS",
                           secrets="PASS"),
        context={"approvals": [], "mergeable": True, "mergeable_state": "clean"},
    )
    assert readiness.verdict == VERDICT_NOT_READY
    assert any("approval" in blocker for blocker in readiness.blockers)


# -- conflicts -------------------------------------------------------------

def test_the_local_merge_probe_wins_over_githubs_flag():
    # GitHub's cached mergeable can lag; the probe actually attempted the merge.
    dimension = conflict_state(
        {"mergeable": True, "mergeable_state": "clean"},
        {"status": "conflicted", "conflict_count": 3},
    )
    assert dimension.status == BLOCKED
    assert "3 conflicted file(s)" in dimension.detail


def test_a_clean_probe_is_ready():
    assert conflict_state({}, {"status": "clean"}).status == READY


def test_a_probe_error_is_a_warning_not_a_block():
    dimension = conflict_state({}, {"status": "error", "reason": "could not fetch base"})
    assert dimension.status == WARN
    assert "could not fetch base" in dimension.detail


def test_githubs_dirty_state_blocks_when_there_is_no_probe():
    assert conflict_state({"mergeable": False, "mergeable_state": "dirty"}, None).status == BLOCKED


def test_a_branch_behind_the_base_is_a_warning():
    dimension = conflict_state({"mergeable": True, "mergeable_state": "behind"}, None)
    assert dimension.status == WARN


def test_an_undetermined_mergeability_is_not_run():
    assert conflict_state({}, None).status == NOT_RUN


def test_githubs_blocked_state_is_a_warning_not_a_second_block():
    # 'blocked' usually means a required check or approval, both of which are
    # already their own dimension; blocking twice would double-count.
    dimension = conflict_state({"mergeable": None, "mergeable_state": "blocked"}, None)
    assert dimension.status == WARN


# -- drafts ----------------------------------------------------------------

def test_a_draft_blocks_by_default_and_can_be_allowed():
    sections = _sections(lint="PASS", test="PASS", build="PASS", security="PASS",
                         secrets="PASS")
    context = {**APPROVED, "draft": True}
    assert assess(sections=sections, context=context).verdict == VERDICT_NOT_READY
    allowed = assess(sections=sections, context=context, config={"allow_draft": True})
    assert allowed.verdict == VERDICT_WARNINGS


def test_a_non_draft_has_no_draft_dimension():
    readiness = assess(sections=_sections(test="PASS"), context=APPROVED)
    assert not any(d.key == "draft" for d in readiness.dimensions)


# -- insight fallback ------------------------------------------------------

def test_an_insight_payload_supplies_a_dimension_with_no_matching_section():
    readiness = assess(
        sections=_sections(test="PASS"),
        context=APPROVED,
        insights={"coverage": {"status": "fail", "summary": "48% of 120 lines"}},
        config={"block_on": ["coverage"]},
    )
    dimension = _dimension(readiness, "coverage")
    assert dimension.status == BLOCKED
    assert dimension.detail == "48% of 120 lines"
    assert readiness.verdict == VERDICT_NOT_READY


def test_a_section_wins_over_a_supplied_insight():
    readiness = assess(
        sections=_sections(coverage="PASS"),
        context=APPROVED,
        insights={"coverage": {"status": "fail", "summary": "ignored"}},
    )
    assert _dimension(readiness, "coverage").status == READY


# -- ordering and rendering ------------------------------------------------

def test_review_and_conflicts_are_listed_first():
    readiness = assess(
        sections=_sections(metadata="PASS", size="PASS", test="PASS"), context=APPROVED,
    )
    assert [d.key for d in readiness.dimensions][:2] == ["review", "conflicts"]


def test_the_markdown_has_a_verdict_a_table_and_a_blocking_list():
    readiness = assess(
        sections=_sections(test="FAIL", secrets="PASS"), context=APPROVED,
    )
    markdown = render_markdown(readiness)
    assert "Merge readiness: NOT READY" in markdown
    assert "| Dimension | Status | Blocking | Detail |" in markdown
    assert "🛑" in markdown


def test_the_markdown_says_so_when_nothing_is_blocking():
    readiness = assess(sections=_sections(test="PASS"), context=APPROVED)
    assert "Nothing is blocking this merge" in render_markdown(readiness)


def test_a_pipe_in_a_detail_cannot_break_the_table():
    readiness = assess(
        sections=[{"name": "Test", "status": "FAIL", "detail": "a | b\nsecond line"}],
        context=APPROVED,
    )
    row = next(line for line in render_markdown(readiness).splitlines() if "Test" in line)
    # Four columns means five cell delimiters; the pipe in the detail is escaped
    # and the newline is folded, so neither adds one.
    assert row.replace("\\|", "").count("|") == 5
    assert "\\| b second line" in row


def test_the_check_text_lists_every_dimension_and_the_blockers():
    readiness = assess(sections=_sections(test="FAIL"), context=APPROVED)
    text = render_check_text(readiness)
    assert "Merge readiness: NOT READY" in text
    assert "Test: BLOCKING" in text
    assert "Blocking:" in text


def test_the_verdict_says_branch_protection_decides():
    readiness = assess(sections=_sections(test="PASS"), context=APPROVED)
    assert any("Branch protection" in note for note in readiness.notes)


def test_to_dict_is_serialisable_and_complete():
    payload = assess(sections=_sections(test="FAIL"), context=APPROVED).to_dict()
    assert set(payload) == {"verdict", "ready", "blockers", "warnings", "notes", "dimensions"}
    assert payload["ready"] is False
    assert payload["dimensions"][0]["key"]
