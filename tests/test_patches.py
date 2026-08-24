"""Patch path protection: the allowlist and the global deny list."""
from __future__ import annotations

import pytest

from scripts.common.errors import AutomationError
from scripts.gitops.patches import check_paths, patch_paths, verify_changed_paths
from scripts.plan import FORBIDDEN_FIX_PATHS, patch_allowlist

DIFF = """diff --git a/backend/app/main.py b/backend/app/main.py
index 111..222 100644
--- a/backend/app/main.py
+++ b/backend/app/main.py
@@ -1,3 +1,3 @@
-old
+new
"""

WORKFLOW_DIFF = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-a
+b
"""

ALLOWED = ["backend/app/**/*.py", "frontend/src/**"]


def test_paths_are_extracted_from_a_diff():
    assert patch_paths(DIFF) == ["backend/app/main.py"]


def test_new_file_diffs_ignore_dev_null():
    diff = "--- /dev/null\n+++ b/backend/app/new.py\n@@ -0,0 +1 @@\n+x\n"
    assert patch_paths(diff) == ["backend/app/new.py"]


def test_absolute_and_traversal_paths_are_discarded():
    diff = "--- a/../../etc/passwd\n+++ b//etc/shadow\n"
    assert patch_paths(diff) == []


def test_an_allowlisted_path_passes():
    assert check_paths(DIFF, ALLOWED, list(FORBIDDEN_FIX_PATHS)) == ["backend/app/main.py"]


def test_a_workflow_path_is_forbidden_even_if_allowlisted():
    with pytest.raises(AutomationError, match="forbidden path"):
        check_paths(WORKFLOW_DIFF, [".github/**"], list(FORBIDDEN_FIX_PATHS))


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "terraform/main.tf",
        "infra/main.tf",
        "helm/values.yaml",
        "backend/Dockerfile",
        "docker-compose.yml",
        "frontend/package-lock.json",
        "backend/requirements.txt",
        "pyproject.toml",
        "secrets/id_rsa.pem",
        "deploy.key",
        ".github/CODEOWNERS",
    ],
)
def test_every_globally_forbidden_path_is_refused(path):
    diff = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(AutomationError, match="forbidden path"):
        check_paths(diff, ["**"], list(FORBIDDEN_FIX_PATHS))


def test_a_path_outside_the_allowlist_is_refused():
    diff = "--- a/docs/readme.md\n+++ b/docs/readme.md\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(AutomationError, match="outside the allowlist"):
        check_paths(diff, ALLOWED, list(FORBIDDEN_FIX_PATHS))


def test_a_patch_with_no_recognisable_paths_is_refused():
    with pytest.raises(AutomationError, match="no recognisable file paths"):
        check_paths("@@ -1 +1 @@\n-a\n+b\n", ALLOWED, [])


def test_an_empty_allowlist_refuses_everything():
    with pytest.raises(AutomationError, match="outside the allowlist"):
        check_paths(DIFF, [], [])


# -- post-apply verification ----------------------------------------------

def _status_runner(paths):
    class Completed:
        returncode = 0
        stdout = "".join(f" M {p}\0" for p in paths)
        stderr = ""

    def runner(args, cwd=None, capture_output=None, text=None):
        return Completed()

    return runner


def test_the_applied_change_set_is_re_checked():
    files = verify_changed_paths(
        ".", ALLOWED, list(FORBIDDEN_FIX_PATHS), runner=_status_runner(["backend/app/main.py"])
    )
    assert files == ["backend/app/main.py"]


def test_an_applied_patch_that_escaped_the_allowlist_is_caught():
    # Defence against a diff whose headers lie about what it touches.
    with pytest.raises(AutomationError, match="outside the allowlist"):
        verify_changed_paths(
            ".", ALLOWED, list(FORBIDDEN_FIX_PATHS), runner=_status_runner(["docs/x.md"])
        )


def test_an_applied_patch_touching_a_forbidden_path_is_caught():
    with pytest.raises(AutomationError, match="forbidden path"):
        verify_changed_paths(
            ".", ["**"], list(FORBIDDEN_FIX_PATHS),
            runner=_status_runner([".github/workflows/ci.yml"]),
        )


# -- allowlist config -----------------------------------------------------

def test_a_config_that_allowlists_a_forbidden_path_is_stripped(capsys):
    allowed = patch_allowlist({"fix_allowed_paths": ["backend/**", ".github/**"]}, "fix_allowed_paths")
    assert allowed == ["backend/**"]
    assert "forbidden" in capsys.readouterr().out


def test_a_missing_allowlist_is_empty_which_disables_patching():
    assert patch_allowlist({}, "fix_allowed_paths") == []


def test_a_malformed_allowlist_is_an_error():
    with pytest.raises(AutomationError, match="must be a list"):
        patch_allowlist({"fix_allowed_paths": "backend/**"}, "fix_allowed_paths")


def test_non_string_entries_are_dropped():
    assert patch_allowlist({"fix_allowed_paths": ["ok/**", 5, "", None]}, "fix_allowed_paths") == [
        "ok/**"
    ]


def test_the_repository_config_in_this_repo_declares_no_forbidden_fix_paths():
    from scripts.common.config import load_repositories

    for name, entry in load_repositories("config/repositories.yml").items():
        for key in ("fix_allowed_paths", "resolve_allowed_paths"):
            for pattern in entry.get(key) or []:
                assert pattern not in FORBIDDEN_FIX_PATHS, f"{name}.{key}: {pattern}"
