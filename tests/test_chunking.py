"""Diff batching: a large PR is reviewed in full, or the gap is reported."""
from __future__ import annotations

from scripts.ai.chunking import (
    merge_findings,
    plan_batches,
    split_by_file,
    split_by_hunk,
)


def file_diff(path: str, lines: int = 5) -> str:
    body = "".join(f"+line {i}\n" for i in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,1 +1,{lines} @@\n{body}"
    )


def test_a_diff_splits_into_one_piece_per_file():
    diff = file_diff("a.py") + file_diff("b.py") + file_diff("c.py")
    assert [path for path, _ in split_by_file(diff)] == ["a.py", "b.py", "c.py"]


def test_an_empty_diff_splits_to_nothing():
    assert split_by_file("") == []
    assert split_by_file("   ") == []


def test_a_headerless_diff_is_kept_as_one_unit():
    assert split_by_file("--- a/x\n+++ b/x\n@@ -1 +1 @@\n+a\n")[0][0] == ""


def test_a_small_file_is_not_split():
    pieces = split_by_hunk("a.py", file_diff("a.py"), 10_000)
    assert len(pieces) == 1
    assert pieces[0].parts == 1


def test_a_large_file_splits_on_hunk_boundaries():
    header = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
    hunks = "".join(f"@@ -{i},1 +{i},2 @@\n+added {i}\n" + "x" * 400 + "\n" for i in range(1, 11))
    pieces = split_by_hunk("big.py", header + hunks, 1200)
    assert len(pieces) > 1
    assert all(piece.path == "big.py" for piece in pieces)
    assert [p.part for p in pieces] == list(range(1, len(pieces) + 1))
    assert all(p.parts == len(pieces) for p in pieces)
    # Every hunk survives somewhere; nothing is dropped.
    combined = "".join(p.text for p in pieces)
    for i in range(1, 11):
        assert f"+added {i}" in combined


def test_one_enormous_hunk_is_sliced_on_line_boundaries():
    header = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
    hunk = "@@ -1,1 +1,200 @@\n" + "".join(f"+line {i}\n" for i in range(200))
    pieces = split_by_hunk("x.py", header + hunk, 500)
    assert len(pieces) > 1
    combined = "".join(p.text for p in pieces)
    # No line was cut in half.
    for line in combined.splitlines():
        assert not line.startswith("+lin") or line.startswith("+line ")
    for i in range(200):
        assert f"+line {i}\n" in combined


def test_a_labelled_piece_names_its_part():
    header = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
    hunks = "".join(f"@@ -{i},1 +{i},2 @@\n" + "y" * 400 + "\n" for i in range(1, 6))
    pieces = split_by_hunk("b.py", header + hunks, 900)
    assert "part 1/" in pieces[0].label


# -- batching --------------------------------------------------------------

def test_a_small_pr_is_a_single_batch():
    plan = plan_batches(file_diff("a.py") + file_diff("b.py"))
    assert len(plan.batches) == 1
    assert plan.complete
    assert plan.covered_paths == ["a.py", "b.py"]


def test_files_are_packed_up_to_the_character_budget():
    diff = "".join(file_diff(f"f{i}.py", lines=20) for i in range(20))
    plan = plan_batches(diff, batch_chars=800, batch_files=50)
    assert len(plan.batches) > 1
    assert plan.complete
    assert len(plan.covered_paths) == 20


def test_the_file_count_per_batch_is_respected():
    diff = "".join(file_diff(f"f{i}.py", lines=1) for i in range(12))
    plan = plan_batches(diff, batch_chars=1_000_000, batch_files=4)
    assert len(plan.batches) == 3
    assert all(len(batch.paths) <= 4 for batch in plan.batches)


def test_every_file_of_a_100_file_pr_reaches_a_batch():
    # This is the case a single 40k-character prompt silently dropped.
    diff = "".join(file_diff(f"f{i}.py", lines=30) for i in range(100))
    plan = plan_batches(diff, batch_chars=30000, batch_files=12, max_batches=50)
    assert plan.complete
    assert len(plan.covered_paths) == 100


def test_exceeding_the_batch_cap_is_reported_not_silent():
    diff = "".join(file_diff(f"f{i}.py", lines=30) for i in range(100))
    plan = plan_batches(diff, batch_chars=2000, batch_files=2, max_batches=3)
    assert not plan.complete
    assert plan.skipped_paths
    assert "were not reviewed" in plan.skipped_reason
    # Skipped and covered never overlap.
    assert not set(plan.skipped_paths) & set(plan.covered_paths)


def test_an_empty_diff_produces_no_batches():
    plan = plan_batches("")
    assert plan.batches == []
    assert plan.complete


def test_batch_text_reassembles_its_pieces():
    plan = plan_batches(file_diff("a.py") + file_diff("b.py"))
    assert "a.py" in plan.batches[0].text
    assert "b.py" in plan.batches[0].text


# -- merging ---------------------------------------------------------------

def test_merge_deduplicates_identical_findings_across_batches():
    finding = {"severity": "high", "path": "a.py", "line": 3, "title": "Same issue"}
    merged = merge_findings([[finding], [dict(finding)]])
    assert len(merged) == 1


def test_merge_is_case_insensitive_on_the_title():
    merged = merge_findings(
        [
            [{"severity": "high", "path": "a.py", "line": 3, "title": "Token expiry"}],
            [{"severity": "high", "path": "a.py", "line": 3, "title": "token expiry"}],
        ]
    )
    assert len(merged) == 1


def test_merge_keeps_distinct_findings():
    merged = merge_findings(
        [
            [{"severity": "high", "path": "a.py", "line": 3, "title": "One"}],
            [{"severity": "high", "path": "a.py", "line": 4, "title": "Two"}],
            [{"severity": "high", "path": "b.py", "line": 3, "title": "One"}],
        ]
    )
    assert len(merged) == 3


def test_merge_sorts_by_severity():
    merged = merge_findings(
        [
            [{"severity": "low", "path": "a.py", "line": 1, "title": "l"}],
            [{"severity": "critical", "path": "a.py", "line": 2, "title": "c"}],
            [{"severity": "medium", "path": "a.py", "line": 3, "title": "m"}],
        ]
    )
    assert [f["severity"] for f in merged] == ["critical", "medium", "low"]


def test_merge_tolerates_empty_groups():
    assert merge_findings([[], None, []]) == []
