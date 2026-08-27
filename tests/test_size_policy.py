"""PR size and risk policy: volume, spread, generated-file exclusion, thresholds."""
from __future__ import annotations

from scripts.insights.size import classify_concerns, evaluate, measure, sensitive_areas


def _context(paths, *, lines_each=10, commits=1):
    return {
        "files": list(paths),
        "file_stats": [
            {"path": path, "status": "modified", "additions": lines_each, "deletions": 0}
            for path in paths
        ],
        "additions": lines_each * len(paths),
        "deletions": 0,
        "commits": [f"commit {n}" for n in range(commits)],
    }


# -- volume ----------------------------------------------------------------

def test_a_small_change_passes_with_no_findings():
    result = evaluate(_context(["backend/app/a.py", "backend/app/b.py"]))
    assert result["status"] == "pass"
    assert result["findings"] == []
    assert result["risk"] == "low"


def test_generated_files_are_counted_separately_not_dropped():
    context = _context(["backend/app/a.py", "package-lock.json"], lines_each=800)
    metrics = measure(context)
    assert metrics["files_reviewable"] == 1
    assert metrics["files_generated"] == 1
    assert metrics["lines_reviewable"] == 800
    assert metrics["lines_generated"] == 800
    assert metrics["lines_total"] == 1600


def test_a_lock_file_alone_does_not_make_a_pull_request_large():
    # 1600 total lines, but only 20 a human has to read.
    context = _context(["backend/app/a.py", "package-lock.json"], lines_each=10)
    context["file_stats"][1]["additions"] = 1580
    result = evaluate(context)
    rules = {f["rule"] for f in result["findings"]}
    assert "size-lines-over-limit" not in rules
    assert "size-lines-large" not in rules


def test_counting_generated_files_can_be_switched_on():
    context = _context(["backend/app/a.py", "package-lock.json"], lines_each=10)
    context["file_stats"][1]["additions"] = 3000
    result = evaluate(context, {"size_policy": {"count_generated": True}})
    assert any(f["rule"] == "size-lines-over-limit" for f in result["findings"])


def test_the_warn_threshold_reports_medium_not_high():
    result = evaluate(_context([f"backend/app/f{n}.py" for n in range(25)], lines_each=1))
    finding = next(f for f in result["findings"] if f["rule"] == "size-files-large")
    assert finding["severity"] == "medium"
    assert result["status"] == "warn"


def test_the_max_threshold_reports_high():
    result = evaluate(_context([f"backend/app/f{n}.py" for n in range(80)], lines_each=1))
    finding = next(f for f in result["findings"] if f["rule"] == "size-files-over-limit")
    assert finding["severity"] == "high"


def test_a_breach_only_fails_when_the_repository_asks_for_it():
    context = _context([f"backend/app/f{n}.py" for n in range(80)], lines_each=1)
    assert evaluate(context)["status"] == "warn"
    strict = evaluate(context, {"size_policy": {"fail_on_violation": True}})
    assert strict["status"] == "fail"


def test_thresholds_are_configurable():
    context = _context(["backend/app/a.py", "backend/app/b.py"], lines_each=1)
    result = evaluate(context, {"size_policy": {"warn_files": 1, "max_files": 0}})
    assert any(f["rule"] == "size-files-large" for f in result["findings"])
    # max_files: 0 disables the hard limit entirely.
    assert not any(f["rule"] == "size-files-over-limit" for f in result["findings"])


def test_the_commit_count_check_is_off_by_default_and_can_be_enabled():
    context = _context(["backend/app/a.py"], commits=30)
    assert not any(f["rule"] == "size-commits-many" for f in evaluate(context)["findings"])
    result = evaluate(context, {"size_policy": {"max_commits": 10}})
    assert any(f["rule"] == "size-commits-many" for f in result["findings"])


def test_ignore_paths_removes_files_from_the_counts_entirely():
    context = _context(["backend/app/a.py", "generated/huge.py"], lines_each=5000)
    metrics = measure(context, ignore_patterns=["generated/**"])
    assert metrics["files_ignored"] == 1
    assert metrics["files_reviewable"] == 1
    assert metrics["lines_reviewable"] == 5000


def test_pr_level_totals_are_used_when_per_file_stats_are_missing():
    metrics = measure({"files": ["a.py"], "additions": 40, "deletions": 10})
    assert metrics["lines_reviewable"] == 50


# -- spread ----------------------------------------------------------------

def test_concerns_are_classified_by_path():
    grouped = classify_concerns([
        "backend/app/a.py", "frontend/src/b.tsx", "terraform/main.tf", "README.md",
    ])
    assert set(grouped) >= {"backend", "frontend", "infrastructure", "documentation"}


def test_mixing_unrelated_areas_is_reported():
    result = evaluate(_context([
        "backend/app/a.py", "frontend/src/b.tsx", "terraform/main.tf",
    ], lines_each=1))
    finding = next(f for f in result["findings"] if f["rule"] == "size-mixed-concerns")
    assert "backend" in finding["title"] and "frontend" in finding["title"]


def test_tests_and_docs_alongside_code_are_not_mixed_concerns():
    # Every good pull request touches these; flagging it would be noise.
    result = evaluate(_context([
        "backend/app/a.py", "tests/test_a.py", "README.md", "requirements.txt",
    ], lines_each=1))
    assert not any(f["rule"] == "size-mixed-concerns" for f in result["findings"])
    assert result["substantive_concerns"] == ["backend"]


def test_the_concern_limit_is_configurable():
    context = _context(["backend/app/a.py", "frontend/src/b.tsx"], lines_each=1)
    result = evaluate(context, {"size_policy": {"max_concerns": 1}})
    assert any(f["rule"] == "size-mixed-concerns" for f in result["findings"])


# -- risk ------------------------------------------------------------------

def test_sensitive_areas_are_named():
    areas = sensitive_areas([".github/workflows/ci.yml", "backend/auth/login.py"])
    assert set(areas) >= {"ci/cd workflows", "authentication"}


def test_touching_a_sensitive_area_forces_high_risk():
    result = evaluate(_context([".github/workflows/ci.yml"], lines_each=1))
    assert result["risk"] == "high"
    assert any(f["rule"] == "size-sensitive-area" for f in result["findings"])


def test_a_large_ordinary_change_is_high_risk_without_a_sensitive_path():
    result = evaluate(_context([f"backend/app/f{n}.py" for n in range(45)], lines_each=1))
    assert result["risk"] == "high"


def test_a_medium_change_is_medium_risk():
    result = evaluate(_context([f"backend/app/f{n}.py" for n in range(18)], lines_each=1))
    assert result["risk"] == "medium"


def test_an_empty_pull_request_does_not_crash():
    result = evaluate({"files": [], "commits": []})
    assert result["status"] == "pass"
    assert result["metrics"]["files_total"] == 0


def test_the_summary_states_the_measured_numbers():
    result = evaluate(_context(["backend/app/a.py"], lines_each=7))
    assert "1 reviewable file(s)" in result["summary"]
    assert "7 changed line(s)" in result["summary"]


def test_the_policy_actually_used_is_reported_back():
    result = evaluate(_context(["a.py"]), {"size_policy": {"max_files": 5}})
    assert result["policy"]["max_files"] == 5
    assert result["policy"]["fail_on_violation"] is False


def test_a_malformed_policy_value_falls_back_to_the_default():
    result = evaluate(_context(["a.py"]), {"size_policy": {"max_files": "lots"}})
    assert result["policy"]["max_files"] == 60
