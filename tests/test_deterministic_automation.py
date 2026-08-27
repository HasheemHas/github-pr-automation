"""End-to-end contracts for the no-AI deterministic automation engine."""
from __future__ import annotations

import json
import subprocess

from scripts.automation.analyze import (
    added_lines,
    analyse,
    analyse_tests,
    generate_metadata,
    review_diff,
)
from scripts.automation.fix import apply_safe_fixes
from scripts.automation.resolve import resolve
from scripts.common.dispatch import plan
from scripts.gitops.probe import probe


CONTEXT = {
    "repository": "org/repo",
    "number": 4,
    "title": "fix login",
    "body": "Closes #12",
    "head_sha": "a" * 40,
    "files": ["backend/auth.py"],
    "commits": ["Prevent token reuse"],
    "additions": 3,
    "deletions": 1,
    "repository_tests": ["tests/test_auth.py"],
    "test_inventory": {"truncated": False},
}

DIFF = """diff --git a/backend/auth.py b/backend/auth.py
--- a/backend/auth.py
+++ b/backend/auth.py
@@ -1 +1,4 @@
 old
+password = "hardcoded-password"
+try:  
+    print("debug")
"""


def test_added_lines_have_right_side_line_numbers():
    assert [(x.path, x.line) for x in added_lines(DIFF)] == [
        ("backend/auth.py", 2), ("backend/auth.py", 3), ("backend/auth.py", 4)
    ]


def test_metadata_uses_only_repository_facts():
    metadata = generate_metadata(CONTEXT)
    assert metadata["change_type"] == "fix"
    assert metadata["related_issues"] == ["#12"]
    assert "backend" in metadata["components"]


def test_metadata_wording_can_come_from_configured_template():
    metadata = generate_metadata(
        CONTEXT,
        {
            "summary": "{subject}",
            "detail_item": "Update `{path}`.",
            "release_note": "{subject}.",
            "testing_item": "Check `{component}`.",
            "review_focus_item": "Review `{path}`.",
            "rollback": "Use the configured rollback runbook.",
            "user_impact": "No known consumer impact.",
            "risk_reasons": {"low": "Small change."},
        },
    )
    assert metadata["summary"] == "Prevent token reuse"
    assert metadata["changes"][0] == "Update `backend/auth.py`."
    assert metadata["recommended_testing"] == ["Check `backend`."]
    assert metadata["rollback"] == "Use the configured rollback runbook."
    assert metadata["user_impact"] == "No known consumer impact."
    assert metadata["risk_reason"] == "Small change."


def test_review_rules_are_reproducible_and_anchorable():
    data = review_diff(CONTEXT, DIFF)
    rules = {finding["rule"] for finding in data["findings"]}
    assert {"secret-literal", "trailing-whitespace", "debug-output"} <= rules
    assert data["complete"] is True


def test_security_scope_excludes_style_rules():
    data = review_diff(CONTEXT, DIFF, security_only=True)
    assert {finding["rule"] for finding in data["findings"]} == {"secret-literal"}


def test_test_gap_matching_finds_existing_test_by_source_stem():
    assert analyse_tests(CONTEXT)["gaps"] == [
        "Production code changed without a test file changing in this pull request."
    ]


def test_all_action_emits_the_existing_report_contract_without_models():
    result = analyse(plan("all"), CONTEXT, DIFF)
    assert result["engine"] == "deterministic-python-rules-v1"
    assert result["metadata"]["status"] == "ok"
    assert result["review"]["status"] == "ok"
    assert result["tests"]["status"] == "ok"
    assert "models" not in result


def test_safe_fixer_edits_only_an_added_allowlisted_line(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    file = target / "backend" / "auth.py"
    file.parent.mkdir()
    file.write_text('old\npassword = "hardcoded-password"\ntry:  \n    print("debug")\n')
    changed = apply_safe_fixes(target, DIFF, ["backend/**/*.py"])
    assert changed == ["backend/auth.py"]
    assert "try:  \n" not in file.read_text()


def test_safe_fixer_replaces_a_new_bare_except(tmp_path):
    target = tmp_path / "target"
    file = target / "src" / "worker.py"
    file.parent.mkdir(parents=True)
    file.write_text("try:\n    work()\nexcept:\n    recover()\n")
    diff = """diff --git a/src/worker.py b/src/worker.py
--- a/src/worker.py
+++ b/src/worker.py
@@ -1,2 +1,4 @@
 try:
     work()
+except:
+    recover()
"""
    assert apply_safe_fixes(target, diff, ["src/**"]) == ["src/worker.py"]
    assert "except Exception:" in file.read_text()


def test_conflict_resolution_takes_the_only_changed_side():
    payload = {"status": "conflicted", "head_sha": "a" * 40, "base_sha": "b" * 40,
               "conflicts": [{"path": "src/a.py", "base": "x\n", "ours": "x\n",
                              "theirs": "y\n", "conflicted_content":
                              "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> base\n",
                              "content_truncated": False}]}
    result = resolve(payload, ["src/**/*.py"])
    assert result["status"] == "ok"
    assert "-<<<<<<< HEAD" in result["data"]["patch"]
    assert "->>>>>>> base" in result["data"]["patch"]


def test_conflict_resolution_refuses_when_both_sides_changed():
    payload = {"status": "conflicted", "conflicts": [{"path": "src/a.py", "base": "x\n",
               "ours": "ours\n", "theirs": "theirs\n", "conflicted_content": "markers\n",
               "content_truncated": False}]}
    result = resolve(payload, ["src/**/*.py"])
    assert result["status"] == "refused"
    assert "require guessing" in result["reason"]


def test_conflict_resolution_semantically_merges_independent_json_keys():
    payload = {"status": "conflicted", "conflicts": [{"path": "src/options.json",
               "base": '{"a": 1}\n', "ours": '{"a": 1, "left": true}\n',
               "theirs": '{"a": 1, "right": true}\n',
               "conflicted_content": '<<<<<<< HEAD\n{"a": 1, "left": true}\n=======\n'
                                     '{"a": 1, "right": true}\n>>>>>>> base\n',
               "content_truncated": False}]}
    result = resolve(payload, ["src/**"])
    assert result["status"] == "ok"
    assert '"left": true' in result["data"]["patch"]
    assert '"right": true' in result["data"]["patch"]


def test_generated_json_resolution_applies_to_a_real_conflicted_git_tree(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    target = tmp_path / "target"

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              check=True).stdout.strip()

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed.mkdir()
    git(seed, "init")
    git(seed, "config", "user.name", "test")
    git(seed, "config", "user.email", "test@example.invalid")
    options = seed / "options.json"
    options.write_text('{"base": true}\n')
    git(seed, "add", "options.json")
    git(seed, "commit", "-m", "base")
    git(seed, "branch", "-M", "main")
    git(seed, "checkout", "-b", "feature")
    options.write_text('{"base": true, "feature": true}\n')
    git(seed, "commit", "-am", "feature")
    head_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "checkout", "main")
    options.write_text('{"base": true, "main": true}\n')
    git(seed, "commit", "-am", "main")
    base_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main", "feature")

    subprocess.run(["git", "clone", "--branch", "feature", str(remote), str(target)],
                   check=True, capture_output=True)
    payload = probe(str(target), base_sha)
    assert payload["status"] == "conflicted"
    assert payload["head_sha"] == head_sha
    result = resolve(payload, ["**/*.json"])
    assert result["status"] == "ok"
    patch_file = tmp_path / "resolution.patch"
    patch_file.write_text(result["data"]["patch"])
    applied = subprocess.run(["git", "apply", str(patch_file)], cwd=target,
                             capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    git(target, "add", "options.json")
    assert git(target, "diff", "--name-only", "--diff-filter=U") == ""
    assert json.loads((target / "options.json").read_text()) == {
        "base": True, "feature": True, "main": True
    }
