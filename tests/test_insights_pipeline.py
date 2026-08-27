"""The analysis job: insights reach analysis.json and enrich the PR metadata."""
from __future__ import annotations

from scripts.automation.analyze import analyse, enrich_metadata, write_summary
from scripts.common.dispatch import plan as build_plan
from scripts.insights import run as insights

CONTEXT = {
    "repository": "org/repo",
    "number": 7,
    "title": "feat: add invoice export",
    "body": "Closes #99",
    "head_sha": "b" * 40,
    "files": [
        "backend/app/api.py",
        "backend/app/migrations/0042_add_status.py",
        "requirements.txt",
        ".env",
    ],
    "file_stats": [
        {"path": "backend/app/api.py", "status": "modified", "additions": 20, "deletions": 4},
        {"path": "backend/app/migrations/0042_add_status.py", "status": "added",
         "additions": 8, "deletions": 0},
        {"path": "requirements.txt", "status": "modified", "additions": 1, "deletions": 1},
        {"path": ".env", "status": "modified", "additions": 1, "deletions": 0},
    ],
    "commits": ["feat(billing): add invoice export", "fix: correct rounding"],
    "additions": 30,
    "deletions": 5,
    "labels": [],
    "repository_tests": ["tests/test_api.py"],
    "test_inventory": {"truncated": False},
}

DIFF = """diff --git a/backend/app/api.py b/backend/app/api.py
--- a/backend/app/api.py
+++ b/backend/app/api.py
@@ -10,2 +10,2 @@
-@router.get("/v1/invoices")
+@router.get("/v2/invoices")
diff --git a/backend/app/migrations/0042_add_status.py b/backend/app/migrations/0042_add_status.py
new file mode 100644
--- /dev/null
+++ b/backend/app/migrations/0042_add_status.py
@@ -0,0 +1,3 @@
+from django.db import migrations
+def forwards(apps, schema_editor):
+    schema_editor.execute("DROP TABLE legacy_invoices;")
diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,1 +1,1 @@
-django==4.2.11
+django==5.0.3
diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -1,0 +1,1 @@
+DB_PASSWORD=Kj8Hn2Qp5Zx9Vb3Lm7Rt4Wy6Ac1Df0Gh
"""


def _plan(action="all", **overrides):
    plan = dict(build_plan(action))
    plan.update(overrides)
    return plan


# -- the runner ------------------------------------------------------------

def test_every_requested_insight_lands_in_the_result():
    result = insights.run_all(_plan(), CONTEXT, DIFF)
    assert set(result) == {"size", "secrets", "dependencies", "migrations", "api",
                           "release_notes"}
    assert all(entry["status"] == "ok" for entry in result.values())


def test_an_unrequested_insight_is_absent_rather_than_empty():
    result = insights.run_all(_plan("size"), CONTEXT, DIFF)
    assert set(result) == {"size"}


def test_one_failing_insight_does_not_sink_the_others(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("detector blew up")

    monkeypatch.setattr(insights.size, "evaluate", explode)
    result = insights.run_all(_plan(), CONTEXT, DIFF)
    assert result["size"]["status"] == "error"
    assert "detector blew up" in result["size"]["error"]
    # Every other insight still produced a result.
    assert result["secrets"]["status"] == "ok"
    assert result["api"]["status"] == "ok"


def test_a_truncated_diff_is_flagged_on_every_insight():
    result = insights.run_all(
        _plan(), CONTEXT, DIFF + "\n[diff truncated by MAX_DIFF_BYTES]\n"
    )
    assert result["api"]["data"]["diff_truncated"] is True
    assert result["secrets"]["data"]["diff_truncated"] is True


def test_release_notes_see_the_other_insights():
    result = insights.run_all(_plan(), CONTEXT, DIFF)
    notes = result["release_notes"]["data"]
    labels = {action["label"] for action in notes["operator_actions"]}
    assert "Database migration" in labels


def test_requested_lists_the_labels_for_the_summary():
    assert insights.requested(_plan("insights")) == [
        "PR size", "Secret scan", "Dependency risk", "Migrations",
        "API compatibility", "Release notes",
    ]


# -- what the insights actually find in this pull request -------------------

def test_the_committed_credential_is_found_and_fails_the_secret_scan():
    secrets = insights.run_all(_plan("secrets"), CONTEXT, DIFF)["secrets"]["data"]
    assert secrets["status"] == "fail"
    assert secrets["hits"][0]["path"] == ".env"
    # The value itself is never carried.
    assert "Kj8Hn2Qp5Zx9Vb3Lm7Rt4Wy6Ac1Df0Gh" not in repr(secrets)


def test_the_dropped_table_is_found_in_the_migration():
    migrations = insights.run_all(_plan("migrations"), CONTEXT, DIFF)["migrations"]["data"]
    assert "drop-table" in migrations["migrations"][0]["risky_operations"]


def test_the_major_django_bump_is_found():
    deps = insights.run_all(_plan("deps"), CONTEXT, DIFF)["dependencies"]["data"]
    assert deps["totals"]["major_bumps"] == 1


def test_the_moved_endpoint_is_reported_as_a_route_change():
    api = insights.run_all(_plan("api"), CONTEXT, DIFF)["api"]["data"]
    assert api["breaking_change"] is True
    # The method comes from the decorator name, so the key is GET, not ANY.
    assert api["routes"]["removed"] == ["GET /v1/invoices"]
    assert api["routes"]["added"] == ["GET /v2/invoices"]


# -- the full analysis result ----------------------------------------------

def test_analyse_carries_the_insights_alongside_the_original_sections():
    result = analyse(_plan(), CONTEXT, DIFF)
    assert {"metadata", "review", "tests"} <= set(result)
    assert {"size", "secrets", "dependencies", "migrations", "api", "release_notes"} \
        <= set(result)
    assert result["engine"] == "deterministic-python-rules-v1"


def test_analyse_without_insight_flags_keeps_the_old_shape():
    result = analyse(_plan("metadata"), CONTEXT, DIFF)
    assert set(result) >= {"metadata"}
    assert "size" not in result and "secrets" not in result


# -- metadata enrichment ---------------------------------------------------

def test_the_measured_risk_replaces_the_path_heuristic():
    result = analyse(_plan(), CONTEXT, DIFF)
    metadata = result["metadata"]["data"]
    assert metadata["risk"] == result["size"]["data"]["risk"]
    # The reason names the files that decided the level, not just a count.
    assert "changed lines" in metadata["risk_reason"]
    assert "reviewable file" in metadata["risk_reason"]
    assert "`.env`" in metadata["risk_reason"]


def test_a_breaking_api_change_sets_the_breaking_change_flag():
    metadata = analyse(_plan(), CONTEXT, DIFF)["metadata"]["data"]
    assert metadata["breaking_change"] is True
    assert metadata["breaking_change_details"]


def test_migration_notes_come_from_the_migration_insight():
    metadata = analyse(_plan(), CONTEXT, DIFF)["metadata"]["data"]
    assert any("0042_add_status.py" in note for note in metadata["migration_notes"])


def test_release_notes_replace_the_placeholder_list():
    metadata = analyse(_plan(), CONTEXT, DIFF)["metadata"]["data"]
    assert "Add invoice export" in " ".join(metadata["release_notes"])


def test_a_committed_credential_goes_to_the_top_of_review_focus():
    metadata = analyse(_plan(), CONTEXT, DIFF)["metadata"]["data"]
    assert "rotate" in metadata["review_focus"][0].lower()


def test_enrichment_is_a_no_op_when_metadata_was_not_generated():
    result = {"size": {"status": "ok", "data": {"risk": "high"}}}
    enrich_metadata(result)          # must not raise
    assert "metadata" not in result


def test_enrichment_ignores_a_failed_insight():
    result = {
        "metadata": {"status": "ok", "data": {"risk": "low", "risk_reason": "original"}},
        "size": {"status": "error", "error": "boom"},
    }
    enrich_metadata(result)
    assert result["metadata"]["data"]["risk_reason"] == "original"


# -- the job summary -------------------------------------------------------

def test_the_job_summary_lists_every_section_and_its_findings(tmp_path, monkeypatch):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    write_summary(analyse(_plan(), CONTEXT, DIFF))
    text = path.read_text(encoding="utf-8")
    for label in ("Metadata", "Secret scan", "Migrations", "API compatibility",
                  "Dependency risk", "PR size", "Release notes"):
        assert label in text
    assert "Generated release notes" in text


def test_the_job_summary_never_contains_a_secret_value(tmp_path, monkeypatch):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    write_summary(analyse(_plan(), CONTEXT, DIFF))
    assert "Kj8Hn2Qp5Zx9Vb3Lm7Rt4Wy6Ac1Df0Gh" not in path.read_text(encoding="utf-8")
