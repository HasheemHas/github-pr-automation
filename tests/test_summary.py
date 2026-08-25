"""GitHub Actions job summary rendering."""
from __future__ import annotations

import pytest

from scripts.common import summary
from scripts.common.commands import CommandResult
from scripts.common.dispatch import plan as build_plan
from scripts.github.report import FAIL, PARTIAL, PASS, Section, write_gate_summary
from scripts.quality.run import write_command_summary


@pytest.fixture
def step_summary(tmp_path, monkeypatch):
    path = tmp_path / "step-summary.md"
    path.touch()
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


# -- primitives ------------------------------------------------------------

def test_write_appends_to_the_step_summary_file(step_summary):
    assert summary.write("# One") is True
    assert summary.write("# Two") is True
    text = step_summary.read_text(encoding="utf-8")
    assert "# One" in text and "# Two" in text
    assert text.index("# One") < text.index("# Two")


def test_write_without_the_env_var_is_a_no_op(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # Scripts must stay runnable locally.
    assert summary.write("# Anything") is False


def test_write_ignores_empty_blocks(step_summary):
    assert summary.write("", "   ", None or "") is False
    assert step_summary.read_text(encoding="utf-8") == ""


def test_an_unwritable_path_never_fails_the_job(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing-dir" / "s.md"))
    assert summary.write("# Block") is False


def test_an_oversized_summary_is_truncated_with_a_note(step_summary):
    summary.write("x" * (summary.MAX_BYTES + 5000))
    text = step_summary.read_text(encoding="utf-8")
    assert "_Summary truncated._" in text
    assert len(text.encode("utf-8")) < summary.MAX_BYTES + 200


def test_table_renders_a_markdown_table():
    rendered = summary.table(["A", "B"], [[1, 2], [3, 4]])
    assert rendered.splitlines() == ["| A | B |", "| --- | --- |", "| 1 | 2 |", "| 3 | 4 |"]


def test_table_with_no_rows_is_empty():
    assert summary.table(["A"], []) == ""


def test_pipes_and_newlines_are_escaped_so_the_table_survives():
    rendered = summary.table(["A"], [["a|b\nc"]])
    assert "a\\|b c" in rendered
    assert len(rendered.splitlines()) == 3


def test_none_becomes_an_empty_cell():
    assert "|  |" in summary.table(["A"], [[None]])


def test_bullets_reports_what_it_trimmed():
    rendered = summary.bullets([f"item {i}" for i in range(30)], limit=5)
    assert rendered.count("\n- ") == 5
    assert "_…and 25 more_" in rendered


def test_bullets_of_nothing_uses_the_placeholder():
    assert summary.bullets([]) == "_none_"
    assert summary.bullets([], empty="_no gaps_") == "_no gaps_"


def test_details_wraps_a_collapsible_block():
    assert summary.details("Title", "body").startswith("<details><summary>Title</summary>")
    assert summary.details("T", "b", open_by_default=True).startswith("<details open>")


def test_definitions_accepts_a_mapping_or_pairs():
    assert summary.definitions({"K": "v"}) == "- **K:** v"
    assert summary.definitions([("K", 1)]) == "- **K:** 1"


@pytest.mark.parametrize(
    "status,expected",
    [("PASS", "✅"), ("FAIL", "❌"), ("PARTIAL", "⚠️"), ("NOT RUN", "⚪"),
     ("ERROR", "🔥"), ("skipped", "⏭️"), ("something-else", "•")],
)
def test_every_status_gets_an_icon(status, expected):
    assert summary.icon(status) == expected


def test_heading_levels_are_clamped():
    assert summary.heading("T", 1) == "# T"
    assert summary.heading("T", 9) == "###### T"
    assert summary.heading("T", 0) == "# T"


# -- command summary -------------------------------------------------------

def test_the_command_summary_lists_every_command(step_summary):
    results = [
        CommandResult("Frontend dependencies", "install", "npm ci", "fail", 1,
                      output_tail="ERR! 404 not found"),
        CommandResult("Frontend lint", "lint", "npm run lint", "skipped",
                      reason="Skipped because Frontend dependencies failed."),
    ]
    write_command_summary(
        "Quality", results, {"status": "fail"},
        {"ready": False, "reason": "no sandbox", "image": ""},
    )
    text = step_summary.read_text(encoding="utf-8")
    assert "Quality — ❌ FAIL" in text
    assert "Frontend dependencies" in text
    assert "Frontend lint" in text
    assert "Skipped because Frontend dependencies failed." in text
    # The failing command's output is attached so the run page explains itself.
    assert "ERR! 404 not found" in text
    assert "host runner" in text
    assert "Credentials in this job" in text


def test_the_command_summary_names_the_sandbox_image(step_summary):
    write_command_summary(
        "Quality", [CommandResult("Lint", "lint", "x", "pass", 0)], {"status": "pass"},
        {"ready": True, "reason": "ready", "image": "example/img:pinned"},
    )
    text = step_summary.read_text(encoding="utf-8")
    assert "container `example/img:pinned`" in text
    assert "✅ PASS" in text


def test_a_passing_command_does_not_dump_its_output(step_summary):
    write_command_summary(
        "Quality", [CommandResult("Lint", "lint", "x", "pass", 0, output_tail="lots of noise")],
        {"status": "pass"}, {"ready": False, "reason": "", "image": ""},
    )
    assert "lots of noise" not in step_summary.read_text(encoding="utf-8")


def test_a_not_run_reason_is_surfaced(step_summary):
    write_command_summary(
        "Security", [], {"status": "not_run", "reason": "No scanners enabled."},
        {"ready": False, "reason": "", "image": ""},
    )
    text = step_summary.read_text(encoding="utf-8")
    assert "⚪ NOT_RUN" in text
    assert "No scanners enabled." in text


# -- gate summary ----------------------------------------------------------

DATA = {
    "counts": {"critical": 1, "high": 2, "medium": 3, "low": 4},
    "findings": [
        {"severity": "critical", "path": "backend/app/auth.py", "line": 74,
         "title": "Token expiry is not validated"}
    ],
    "gaps": ["No test covers an expired token"],
    "metadata": {"summary": "s"},
    "review": {"batches": 2, "complete": True},
    "tests": {},
}


def _gate(step_summary, **kwargs):
    defaults = dict(
        full_name="org/repo",
        number=7,
        sections=[Section("Review", FAIL, "1 critical"), Section("Lint", PASS, "ok")],
        data=DATA,
        verdict=FAIL,
        labels=["backend", "high-risk"],
        reviewers={"users": ["dev"], "teams": [], "notes": []},
        metadata_outcome={"title_updated": True, "new_title": "feat: real title",
                          "body_updated": True},
        inline_outcome={"posted": 1, "skipped_existing": 0, "rejected": 0, "overflow": []},
        unanchored=[],
        notes=[],
    )
    defaults.update(kwargs)
    write_gate_summary(**defaults)
    return step_summary.read_text(encoding="utf-8")


def test_the_gate_summary_leads_with_the_verdict(step_summary):
    text = _gate(step_summary)
    assert text.startswith("## ❌ Quality Gate: FAIL")


def test_the_gate_summary_links_the_pull_request(step_summary):
    assert "https://github.com/org/repo/pull/7" in _gate(step_summary)


def test_the_gate_summary_tabulates_every_section(step_summary):
    text = _gate(step_summary)
    assert "| Review | ❌ FAIL | 1 critical |" in text
    assert "| Lint | ✅ PASS | ok |" in text


def test_the_gate_summary_counts_findings_by_severity(step_summary):
    text = _gate(step_summary)
    assert "| Critical | High | Medium | Low/Info | Test gaps |" in text
    assert "| 1 | 2 | 3 | 4 | 1 |" in text


def test_the_gate_summary_states_what_was_written(step_summary):
    text = _gate(step_summary)
    assert "replaced → `feat: real title`" in text
    assert "`backend`, `high-risk`" in text
    assert "@dev" in text
    assert "1 posted" in text


def test_the_gate_summary_states_an_unchanged_title_and_why(step_summary):
    text = _gate(
        step_summary,
        metadata_outcome={"title_updated": False, "body_updated": True,
                          "title_reason": "Existing title is descriptive; kept."},
    )
    assert "unchanged — Existing title is descriptive; kept." in text


def test_findings_are_expanded_on_a_failing_gate(step_summary):
    text = _gate(step_summary)
    assert "<details open><summary>Findings (1)</summary>" in text
    assert "Token expiry is not validated" in text


def test_findings_stay_collapsed_on_a_passing_gate(step_summary):
    text = _gate(step_summary, verdict=PASS)
    assert "<details><summary>Findings (1)</summary>" in text


def test_unanchorable_findings_are_listed_with_their_reason(step_summary):
    text = _gate(
        step_summary,
        unanchored=[{"path": "a.py", "line": 999, "title": "Bad anchor",
                     "unanchored_reason": "line 999 is not part of the diff"}],
    )
    assert "not anchorable" in text
    assert "line 999 is not part of the diff" in text


def test_notes_are_surfaced(step_summary):
    text = _gate(step_summary, notes=["Reviewer 'ghost' could not be requested."])
    assert "Notes (1)" in text
    assert "ghost" in text


def test_a_partial_verdict_uses_the_warning_icon(step_summary):
    assert "⚠️ Quality Gate: PARTIAL" in _gate(step_summary, verdict=PARTIAL)


def test_codeowners_deference_is_stated(step_summary):
    text = _gate(step_summary, reviewers={"deferred_to_codeowners": True, "users": [],
                                          "teams": [], "notes": []})
    assert "deferred to CODEOWNERS" in text


def test_the_gate_summary_always_reminds_about_human_approval(step_summary):
    assert "Human approval is still required" in _gate(step_summary)


def test_the_gate_summary_survives_a_metadata_only_run(step_summary):
    # Every section NOT RUN except metadata; no findings, no gaps.
    text = _gate(
        step_summary,
        sections=[Section("Metadata", PASS, "Summary generated"),
                  Section("Review", "NOT RUN", "Not requested for this action.")],
        data={"counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
              "findings": [], "gaps": [], "metadata": {"summary": "s"}, "review": {},
              "tests": {}},
        verdict=PASS,
        labels=[],
        reviewers={"users": [], "teams": [], "notes": []},
        inline_outcome={"posted": 0},
    )
    assert "✅ Quality Gate: PASS" in text
    assert "⚪ NOT RUN" in text
    assert "none" in text


def test_the_ai_analysis_summary_renders(step_summary):
    from scripts.ai.analyze import write_summary

    write_summary(
        {
            "models": {"metadata": "vendor/meta", "review": "vendor/review"},
            "metadata": {"status": "ok", "data": {"summary": "Adds validation.",
                                                  "title": "feat: add validation",
                                                  "risk": "low", "change_type": "feature",
                                                  "changes": ["Added a check"]}},
            "review": {"status": "ok", "data": {"findings": [], "batches": 3,
                                                "succeeded_batches": 2, "complete": False,
                                                "unreviewed_paths": ["big.py"]}},
            "tests": {"status": "error", "error": "provider down", "data": None},
        }
    )
    text = step_summary.read_text(encoding="utf-8")
    assert "🤖 AI analysis" in text
    assert "`vendor/meta`" in text
    assert "Adds validation." in text
    assert "2/3 batch(es)" in text
    assert "1 file(s) NOT reviewed" in text
    assert "[!WARNING]" in text
    assert "`big.py`" in text
    assert "🔥 error" in text
    assert "provider down" in text
