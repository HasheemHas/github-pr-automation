"""Right-side diff mapping: the authority on where an inline comment may go."""
from __future__ import annotations

from scripts.github.diffmap import anchor, parse_diff, split_anchored

DIFF = """diff --git a/backend/app/auth.py b/backend/app/auth.py
index 1111111..2222222 100644
--- a/backend/app/auth.py
+++ b/backend/app/auth.py
@@ -70,7 +70,9 @@ def decode(token):
     payload = jwt.decode(token, key)
     if not payload:
         raise Invalid()
-    return payload
+    if payload["exp"] < now():
+        raise Expired()
+    return payload


 def issue(user):
"""

MULTI = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 one
+two
 three
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -10,1 +10,2 @@
 ten
+eleven
"""

DELETED = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""

NEW_FILE = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+one
+two
+three
"""


def test_added_lines_get_the_right_new_file_numbers():
    entry = parse_diff(DIFF)["backend/app/auth.py"]
    # Hunk starts at new line 70: 70,71,72 context, then three added lines.
    assert entry.added == {73, 74, 75}


def test_context_lines_are_commentable_too():
    entry = parse_diff(DIFF)["backend/app/auth.py"]
    assert 70 in entry.context
    assert 70 in entry.commentable
    assert 73 in entry.commentable


def test_removed_lines_do_not_advance_the_right_side_counter():
    entry = parse_diff(DIFF)["backend/app/auth.py"]
    # The removed "return payload" must not consume a right-side line number, so
    # the first added line is 73 and the last is 75, not 74 and 76.
    assert min(entry.added) == 73
    assert max(entry.added) == 75
    # Line 76 onwards is the trailing context after the hunk's changes.
    assert 76 in entry.context


def test_multiple_files_are_mapped_independently():
    diff_map = parse_diff(MULTI)
    assert set(diff_map) == {"a.py", "b.py"}
    assert diff_map["a.py"].added == {2}
    assert diff_map["b.py"].added == {11}


def test_a_deleted_file_has_no_right_side():
    assert parse_diff(DELETED) == {}


def test_a_new_file_maps_every_line():
    assert parse_diff(NEW_FILE)["new.py"].added == {1, 2, 3}


def test_an_empty_diff_maps_to_nothing():
    assert parse_diff("") == {}
    assert parse_diff("   ") == {}


# -- anchoring -------------------------------------------------------------

def test_a_line_in_the_diff_anchors():
    diff_map = parse_diff(DIFF)
    assert anchor(diff_map, "backend/app/auth.py", 73) == 73


def test_a_line_outside_the_diff_does_not_anchor():
    diff_map = parse_diff(DIFF)
    # The model's favourite failure: a plausible line that is not in the diff.
    assert anchor(diff_map, "backend/app/auth.py", 500) is None
    assert anchor(diff_map, "backend/app/auth.py", 1) is None


def test_a_path_outside_the_diff_does_not_anchor():
    assert anchor(parse_diff(DIFF), "backend/app/other.py", 73) is None


def test_a_numeric_string_line_is_accepted():
    assert anchor(parse_diff(DIFF), "backend/app/auth.py", "73") == 73


def test_junk_line_values_do_not_anchor():
    diff_map = parse_diff(DIFF)
    for value in (None, "", "abc", 0, -5, True, 3.5, [73]):
        assert anchor(diff_map, "backend/app/auth.py", value) is None


def test_split_anchored_separates_verified_from_unanchorable():
    diff_map = parse_diff(DIFF)
    findings = [
        {"severity": "high", "path": "backend/app/auth.py", "line": 73, "title": "real"},
        {"severity": "high", "path": "backend/app/auth.py", "line": 999, "title": "bad line"},
        {"severity": "high", "path": "other.py", "line": 1, "title": "bad path"},
        {"severity": "high", "path": "", "line": 1, "title": "no path"},
    ]
    inline, summary = split_anchored(diff_map, findings)
    assert [f["title"] for f in inline] == ["real"]
    assert [f["title"] for f in summary] == ["bad line", "bad path", "no path"]
    assert "not part of the diff" in summary[0]["unanchored_reason"]
    assert summary[1]["unanchored_reason"] == "path not in this diff"
    assert summary[2]["unanchored_reason"] == "no path"


def test_split_anchored_never_invents_a_line():
    # A finding one line off is demoted, not snapped: a wrong anchor is worse
    # than the same finding in the summary comment.
    diff_map = parse_diff(MULTI)
    inline, summary = split_anchored(
        diff_map, [{"path": "b.py", "line": 12, "title": "off by one"}]
    )
    assert inline == []
    assert len(summary) == 1


def test_split_anchored_preserves_the_original_finding_fields():
    diff_map = parse_diff(MULTI)
    inline, _ = split_anchored(
        diff_map,
        [{"path": "a.py", "line": 2, "title": "t", "severity": "high", "body": "b"}],
    )
    assert inline[0]["severity"] == "high"
    assert inline[0]["body"] == "b"
