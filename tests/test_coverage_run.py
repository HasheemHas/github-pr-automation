"""Changed-line coverage: line intersection, path matching, threshold logic."""
from __future__ import annotations

from scripts.coverage.run import analyse, evaluate, match_report_path, measurable


def _diff(path, first_line, count):
    body = "".join(f"+line {n}\n" for n in range(count))
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{first_line},0 +{first_line},{count} @@\n{body}"
    )


# -- what counts as measurable --------------------------------------------

def test_source_files_are_measurable_and_tests_are_not():
    from scripts.coverage.run import DEFAULT_EXCLUDE, DEFAULT_MEASURABLE

    assert measurable("app/main.py", extensions=DEFAULT_MEASURABLE, exclude=DEFAULT_EXCLUDE)
    assert not measurable("tests/test_main.py", extensions=DEFAULT_MEASURABLE,
                          exclude=DEFAULT_EXCLUDE)
    assert not measurable("README.md", extensions=DEFAULT_MEASURABLE,
                          exclude=DEFAULT_EXCLUDE)
    assert not measurable("app/migrations/0001_init.py", extensions=DEFAULT_MEASURABLE,
                          exclude=DEFAULT_EXCLUDE)


# -- path matching ---------------------------------------------------------

def test_an_exact_path_matches():
    assert match_report_path("app/main.py", {"app/main.py": {}}) == "app/main.py"


def test_a_report_path_with_a_build_prefix_matches_by_suffix():
    assert match_report_path("app/main.py", {"build/src/app/main.py": {}}) == \
        "build/src/app/main.py"


def test_a_repo_path_deeper_than_the_report_path_matches():
    assert match_report_path("backend/app/main.py", {"app/main.py": {}}) == "app/main.py"


def test_a_shared_filename_alone_is_not_enough():
    # Two index.ts files in different packages must not be conflated.
    assert match_report_path("packages/a/index.ts", {"packages/b/index.ts": {}}) is None


def test_the_longest_shared_suffix_wins():
    report = {"other/main.py": {}, "src/app/main.py": {}}
    assert match_report_path("app/main.py", report) == "src/app/main.py"


def test_no_match_returns_none():
    assert match_report_path("app/main.py", {"unrelated/other.py": {}}) is None


# -- the intersection ------------------------------------------------------

def test_covered_and_uncovered_changed_lines_are_counted():
    diff = _diff("app/main.py", 10, 4)          # adds lines 10..13
    hits = {"app/main.py": {10: 1, 11: 0, 12: 3, 13: 0}}
    result = analyse(diff, hits)
    assert result["measured_lines"] == 4
    assert result["covered_lines"] == 2
    assert result["changed_line_coverage"] == 50.0
    assert result["files"][0]["uncovered_lines"] == [11, 13]


def test_added_lines_absent_from_the_report_are_not_executable():
    # A blank line or a comment is not a statement; counting it as uncovered
    # would understate coverage for no reason.
    diff = _diff("app/main.py", 10, 4)
    hits = {"app/main.py": {10: 1, 12: 1}}
    result = analyse(diff, hits)
    assert result["measured_lines"] == 2
    assert result["covered_lines"] == 2
    assert result["changed_line_coverage"] == 100.0
    assert result["files"][0]["not_executable"] == 2


def test_only_changed_lines_count_not_the_whole_file():
    diff = _diff("app/main.py", 10, 2)
    hits = {"app/main.py": {1: 0, 2: 0, 3: 0, 10: 1, 11: 1}}
    result = analyse(diff, hits)
    # The pre-existing uncovered lines 1-3 are irrelevant to this pull request.
    assert result["changed_line_coverage"] == 100.0
    assert result["measured_lines"] == 2


def test_a_file_in_no_report_is_reported_as_unmeasured_not_uncovered():
    result = analyse(_diff("app/new.py", 1, 5), {"app/other.py": {1: 1}})
    assert result["unmeasured_files"][0]["path"] == "app/new.py"
    assert result["unmeasured_lines"] == 5
    # It is excluded from the percentage rather than scoring zero.
    assert result["denominator"] == 0
    assert result["changed_line_coverage"] is None


def test_unmeasured_files_can_be_counted_as_uncovered():
    result = analyse(
        _diff("app/new.py", 1, 4), {"app/other.py": {1: 1}},
        treat_unmeasured_as_uncovered=True,
    )
    assert result["denominator"] == 4
    assert result["changed_line_coverage"] == 0.0


def test_test_files_are_skipped_not_measured():
    result = analyse(_diff("tests/test_main.py", 1, 3), {})
    assert result["skipped_files"] == ["tests/test_main.py"]
    assert result["unmeasured_files"] == []


def test_a_deleted_file_is_ignored():
    diff = (
        "diff --git a/app/gone.py b/app/gone.py\ndeleted file mode 100644\n"
        "--- a/app/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    result = analyse(diff, {})
    assert result["files"] == [] and result["unmeasured_files"] == []


def test_files_are_ordered_worst_coverage_first():
    diff = _diff("app/good.py", 1, 2) + _diff("app/bad.py", 1, 2)
    hits = {"app/good.py": {1: 1, 2: 1}, "app/bad.py": {1: 0, 2: 0}}
    result = analyse(diff, hits)
    assert [f["path"] for f in result["files"]] == ["app/bad.py", "app/good.py"]


# -- threshold evaluation --------------------------------------------------

def _analysis(percent, denominator=100, unmeasured=()):
    covered = int(round(denominator * percent / 100)) if denominator else 0
    return {
        "changed_line_coverage": percent,
        "denominator": denominator,
        "measured_lines": denominator,
        "covered_lines": covered,
        "uncovered_lines": denominator - covered,
        "unmeasured_files": list(unmeasured),
    }


def test_no_threshold_means_measure_and_report_only():
    decision = evaluate(_analysis(12.0), {"min_changed_line_coverage": 0})
    assert decision["status"] == "pass"
    assert "no threshold configured" in decision["reason"]


def test_coverage_under_the_threshold_fails():
    decision = evaluate(_analysis(60.0), {"min_changed_line_coverage": 80})
    assert decision["status"] == "fail"
    assert "under the 80% threshold" in decision["reason"]


def test_coverage_at_the_threshold_passes():
    decision = evaluate(_analysis(80.0), {"min_changed_line_coverage": 80})
    assert decision["status"] == "pass"


def test_fail_under_can_be_downgraded_to_a_warning():
    decision = evaluate(
        _analysis(60.0), {"min_changed_line_coverage": 80, "fail_under": False}
    )
    assert decision["status"] == "warn"


def test_a_tiny_change_is_not_judged_on_a_percentage():
    # 2 of 3 lines is 66%, which says nothing useful about a three-line change.
    decision = evaluate(
        _analysis(66.7, denominator=3),
        {"min_changed_line_coverage": 80, "min_changed_lines": 10},
    )
    assert decision["status"] == "pass"
    assert "floor for enforcing a percentage" in decision["reason"]


def test_nothing_measurable_is_not_run_rather_than_a_pass():
    decision = evaluate(_analysis(None, denominator=0), {"min_changed_line_coverage": 80})
    assert decision["status"] == "not_run"


def test_unmeasured_files_with_nothing_measurable_is_a_warning():
    analysis = _analysis(None, denominator=0, unmeasured=[{"path": "a.py", "added_lines": 4}])
    decision = evaluate(analysis, {"min_changed_line_coverage": 80})
    assert decision["status"] == "warn"
    assert decision["notes"]


def test_a_pass_with_unmeasured_files_is_downgraded_to_a_warning():
    analysis = _analysis(100.0, unmeasured=[{"path": "a.py", "added_lines": 4}])
    decision = evaluate(analysis, {"min_changed_line_coverage": 80})
    assert decision["status"] == "warn"
    assert "not present in any coverage report" in decision["notes"][0]
