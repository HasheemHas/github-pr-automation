"""Diff parsing: added and removed sides, file status, renames, binaries."""
from __future__ import annotations

from scripts.insights.changes import (
    all_added,
    all_removed,
    file_stats,
    is_truncated,
    parse_changes,
)

SIMPLE = """diff --git a/app/main.py b/app/main.py
index 111..222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -10,3 +10,4 @@ def handler():
     existing = 1
-    removed_line = 2
+    added_line = 2
+    another = 3
     trailing = 4
"""

NEW_FILE = """diff --git a/app/new.py b/app/new.py
new file mode 100644
index 000..222
--- /dev/null
+++ b/app/new.py
@@ -0,0 +1,2 @@
+first
+second
"""

DELETED_FILE = """diff --git a/app/gone.py b/app/gone.py
deleted file mode 100644
index 222..000
--- a/app/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-first
-second
"""

RENAMED = """diff --git a/old/name.py b/new/name.py
similarity index 96%
rename from old/name.py
rename to new/name.py
--- a/old/name.py
+++ b/new/name.py
@@ -1,2 +1,2 @@
-old line
+new line
"""

BINARY = """diff --git a/logo.png b/logo.png
index 111..222 100644
Binary files a/logo.png and b/logo.png differ
"""


def test_added_and_removed_lines_carry_the_right_numbers():
    changes = parse_changes(SIMPLE)
    change = changes["app/main.py"]
    assert [(line.line, line.text) for line in change.added] == [
        (11, "    added_line = 2"), (12, "    another = 3")
    ]
    # The removed line keeps its number on the OLD side.
    assert [(line.line, line.text) for line in change.removed] == [
        (11, "    removed_line = 2")
    ]
    assert change.is_modified
    assert not change.is_new


def test_context_lines_advance_both_sides():
    change = parse_changes(SIMPLE)["app/main.py"]
    # The trailing context line follows two additions and one removal, so the two
    # sides must have diverged by exactly one.
    assert change.added[-1].line == 12


def test_a_new_file_is_flagged_as_new():
    change = parse_changes(NEW_FILE)["app/new.py"]
    assert change.is_new
    assert not change.is_deleted
    assert len(change.added) == 2
    assert change.removed == []


def test_a_deleted_file_keeps_its_entry_and_its_removed_lines():
    # The API compatibility check needs the removed side of a deleted file.
    change = parse_changes(DELETED_FILE)["app/gone.py"]
    assert change.is_deleted
    assert [line.text for line in change.removed] == ["first", "second"]
    assert change.added == []


def test_a_rename_is_keyed_by_the_new_path_and_remembers_the_old_one():
    changes = parse_changes(RENAMED)
    assert "new/name.py" in changes
    change = changes["new/name.py"]
    assert change.old_path == "old/name.py"
    assert change.is_rename


def test_a_binary_file_is_flagged_and_has_no_lines():
    change = parse_changes(BINARY)["logo.png"]
    assert change.is_binary
    assert change.added == [] and change.removed == []


def test_multiple_files_in_one_diff():
    changes = parse_changes(SIMPLE + NEW_FILE + DELETED_FILE)
    assert set(changes) == {"app/main.py", "app/new.py", "app/gone.py"}
    assert len(list(all_added(changes))) == 4
    assert len(list(all_removed(changes))) == 3


def test_an_empty_or_garbage_diff_yields_nothing():
    assert parse_changes("") == {}
    assert parse_changes("not a diff at all\njust text\n") == {}


def test_no_newline_marker_does_not_shift_line_numbers():
    diff = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1,2 +1,2 @@\n-old\n\\ No newline at end of file\n+new\n"
    )
    change = parse_changes(diff)["a.txt"]
    assert [line.line for line in change.added] == [1]


def test_truncation_marker_is_detected():
    assert is_truncated("x\n[diff truncated by MAX_DIFF_BYTES]\n")
    assert not is_truncated(SIMPLE)


def test_file_stats_are_indexed_by_path():
    stats = file_stats({"file_stats": [
        {"path": "a.py", "additions": 3, "deletions": 1},
        {"path": "", "additions": 9},
        "not a mapping",
    ]})
    assert set(stats) == {"a.py"}
    assert stats["a.py"]["additions"] == 3


def test_added_text_joins_the_added_side_only():
    change = parse_changes(SIMPLE)["app/main.py"]
    assert "added_line" in change.added_text
    assert "removed_line" not in change.added_text
    assert "removed_line" in change.removed_text


def test_to_dict_reports_the_status():
    assert parse_changes(NEW_FILE)["app/new.py"].to_dict()["status"] == "added"
    assert parse_changes(DELETED_FILE)["app/gone.py"].to_dict()["status"] == "deleted"
    assert parse_changes(RENAMED)["new/name.py"].to_dict()["status"] == "renamed"
    assert parse_changes(SIMPLE)["app/main.py"].to_dict()["status"] == "modified"
