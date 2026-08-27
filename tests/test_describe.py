"""Description derivation: noise, per-file narrative, test plan, user impact.

The defect these guard against: a description built from a template rendered per
path, which listed build artefacts as work, repeated itself between At a Glance
and Details, and offered a test plan naming a directory instead of a test.
"""
from __future__ import annotations

from scripts.insights import describe
from scripts.insights.changes import parse_changes


def _diff(path, added=(), removed=(), new=False, deleted=False, binary=False):
    header = f"diff --git a/{path} b/{path}\n"
    if binary:
        return header + f"index 111..222 100644\nBinary files a/{path} and b/{path} differ\n"
    if new:
        header += "new file mode 100644\n--- /dev/null\n" + f"+++ b/{path}\n"
    elif deleted:
        header += f"deleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
    else:
        header += f"--- a/{path}\n+++ b/{path}\n"
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return header + f"@@ -1,{len(removed) or 1} +1,{len(added) or 1} @@\n{body}"


def _change(path, **kwargs):
    return parse_changes(_diff(path, **kwargs))[path]


# -- noise -----------------------------------------------------------------

def test_build_artefacts_are_recognised_as_noise():
    for path in ("scripts/__pycache__/x.cpython-310.pyc", "a/b.pyc",
                 "package-lock.json", "vendor/pkg/file.go", "dist/bundle.js",
                 "app/__snapshots__/x.snap", "target/App.class"):
        assert describe.is_noise(path) is not None, path


def test_real_source_is_not_noise():
    for path in ("scripts/app.py", "src/index.ts", ".github/workflows/ci.yml",
                 "requirements.txt", "README.md"):
        assert describe.is_noise(path) is None, path


def test_noise_is_grouped_by_kind_with_a_directory_hint():
    groups = describe.group_noise([
        "scripts/__pycache__/a.pyc", "scripts/__pycache__/b.pyc",
        "tests/__pycache__/c.pyc", "package-lock.json",
    ])
    labels = {g["label"]: g for g in groups}
    assert labels["compiled Python bytecode"]["count"] == 3
    assert set(labels["compiled Python bytecode"]["directories"]) == {
        "scripts/__pycache__", "tests/__pycache__"
    }
    assert labels["dependency lock file"]["count"] == 1


def test_the_bytecode_advice_says_what_to_do_about_it():
    groups = describe.group_noise(["scripts/__pycache__/a.pyc"])
    assert ".gitignore" in groups[0]["advice"]


def test_noise_files_never_appear_as_individual_detail_bullets():
    paths = ["app/main.py", "app/__pycache__/main.cpython-310.pyc"]
    changes = parse_changes(
        _diff("app/main.py", added=["def handler():"])
        + _diff("app/__pycache__/main.cpython-310.pyc", binary=True)
    )
    bullets = describe.detail_bullets(changes, {"files": paths})
    assert not any("main.cpython-310.pyc" in bullet for bullet in bullets)
    assert any("compiled Python bytecode" in bullet for bullet in bullets)


# -- per-file narrative ----------------------------------------------------

def test_a_python_file_is_described_by_the_functions_it_adds():
    change = _change("app/api.py", added=["def create_invoice(payload):",
                                          "def delete_invoice(id):"])
    sentence = describe.describe_file(change)
    assert "adds" in sentence
    assert "`create_invoice`" in sentence and "`delete_invoice`" in sentence


def test_removed_symbols_are_named():
    change = _change("app/api.py", removed=["def legacy_handler():"])
    assert "removes `legacy_handler`" in describe.describe_file(change)


def test_a_symbol_present_on_both_sides_is_not_reported_as_removed():
    change = _change("app/api.py", added=["def handler(a, b):"],
                     removed=["def handler(a):"])
    sentence = describe.describe_file(change)
    assert "removes" not in sentence
    assert "`handler`" in sentence


def test_private_helpers_are_not_named():
    change = _change("app/api.py", added=["def _internal():", "def public():"])
    sentence = describe.describe_file(change)
    assert "`public`" in sentence
    assert "_internal" not in sentence


def test_a_class_is_described_as_a_class():
    change = _change("app/models.py", added=["class Invoice:"])
    assert "class `Invoice`" in describe.describe_file(change)


def test_a_typescript_type_is_recognised():
    change = _change("src/types.ts", added=["export interface Invoice {"])
    assert "`Invoice`" in describe.describe_file(change)


def test_a_workflow_is_described_by_its_jobs_triggers_and_actions():
    change = _change(".github/workflows/ci.yml", added=[
        "on:", "  pull_request:", "jobs:", "  build:", "    steps:",
        "      - uses: actions/checkout@v5",
    ])
    sentence = describe.describe_file(change)
    assert "adds the `build` job" in sentence
    assert "triggers on `pull_request`" in sentence
    assert "actions/checkout@v5" in sentence


def test_a_single_job_is_not_written_as_job_s():
    change = _change(".github/workflows/ci.yml", added=["jobs:", "  build:"])
    sentence = describe.describe_file(change)
    assert "job(s)" not in sentence
    assert "`build` job" in sentence


def test_multiple_jobs_are_pluralised():
    change = _change(".github/workflows/ci.yml",
                     added=["jobs:", "  build:", "  test:"])
    assert "jobs" in describe.describe_file(change)


def test_a_config_file_is_described_by_its_keys():
    change = _change("config/app.yml", added=["timeout: 30", "retries: 3"])
    sentence = describe.describe_file(change)
    assert "`timeout`" in sentence and "`retries`" in sentence


def test_a_markdown_file_is_described_by_its_headings():
    change = _change("docs/guide.md", added=["## Installation", "## Configuration"])
    sentence = describe.describe_file(change)
    assert "documents" in sentence
    assert "Installation" in sentence


def test_a_new_file_says_so():
    change = _change("app/new.py", added=["def thing():"], new=True)
    assert "new file" in describe.describe_file(change)


def test_a_deleted_file_says_so():
    change = _change("app/gone.py", removed=["def thing():"], deleted=True)
    assert "deleted" in describe.describe_file(change)


def test_a_binary_file_is_not_guessed_at():
    change = _change("logo.png", binary=True)
    assert "binary file changed" in describe.describe_file(change)


def test_a_file_with_no_recognisable_content_falls_back_to_its_kind():
    change = _change("styles/app.css", added=["  color: red;"])
    assert "styles updated" in describe.describe_file(change)


def test_the_line_counts_come_from_github_stats_when_given():
    change = _change("app/api.py", added=["def a():"])
    sentence = describe.describe_file(change, {"additions": 40, "deletions": 7})
    assert "(+40/-7)" in sentence


# -- detail bullets --------------------------------------------------------

def test_bullets_are_grouped_by_component_when_there_is_more_than_one():
    changes = parse_changes(
        _diff("backend/api.py", added=["def a():"])
        + _diff("frontend/app.ts", added=["export function b() {"])
    )
    bullets = describe.detail_bullets(
        changes, {"files": ["backend/api.py", "frontend/app.ts"]}
    )
    assert any(b.startswith("**backend**") for b in bullets)
    assert any(b.startswith("**frontend**") for b in bullets)


def test_a_single_component_is_not_given_a_group_heading():
    changes = parse_changes(_diff("backend/api.py", added=["def a():"]))
    bullets = describe.detail_bullets(changes, {"files": ["backend/api.py"]})
    assert not any(b.startswith("**") for b in bullets)


def test_bullets_are_capped_and_say_what_was_trimmed():
    paths = [f"app/f{n}.py" for n in range(30)]
    changes = parse_changes("".join(_diff(p, added=["def a():"]) for p in paths))
    bullets = describe.detail_bullets(changes, {"files": paths}, limit=5)
    assert any("and 25 more" in b for b in bullets)


# -- at a glance -----------------------------------------------------------

def test_at_a_glance_is_prose_not_the_file_list():
    changes = parse_changes(_diff("app/api.py", added=["def a():"]))
    text = describe.at_a_glance(
        changes, {"files": ["app/api.py"], "additions": 12, "deletions": 3},
        change_type="feature", risk="low", risk_reason="small change",
    )
    assert "Adds functionality" in text
    assert "app/api.py" not in text
    assert "Risk: Low" in text
    assert "small change" in text


def test_at_a_glance_reports_the_noise_count_separately():
    paths = ["app/api.py", "app/__pycache__/api.pyc", "package-lock.json"]
    changes = parse_changes(_diff("app/api.py", added=["def a():"]))
    text = describe.at_a_glance(changes, {"files": paths})
    assert "1 file" in text
    assert "A further 2 files" in text


def test_at_a_glance_surfaces_the_insight_highlights():
    changes = parse_changes(_diff("app/api.py", added=["def a():"]))
    text = describe.at_a_glance(
        changes, {"files": ["app/api.py"]},
        insights={
            "secrets": {"status": "ok", "data": {"hits": [{"path": ".env", "line": 1}]}},
            "migrations": {"status": "ok", "data": {"migrations": [
                {"path": "m.sql", "risky_operations": ["drop-table"]}]}},
        },
    )
    assert "1 credential-shaped value was added" in text
    assert "1 database migration" in text
    assert "destructive" in text


def test_a_workflow_pin_is_not_announced_as_a_new_dependency():
    # `uses: actions/checkout@v5` is a dependency, but calling it one next to
    # "2 CI workflow files changed" reads as a package install.
    changes = parse_changes(_diff(".github/workflows/ci.yml", added=["jobs:", "  build:"]))
    text = describe.at_a_glance(
        changes, {"files": [".github/workflows/ci.yml"]},
        insights={"dependencies": {"status": "ok", "data": {
            "ecosystems": {"actions": {}}, "totals": {"added": 2},
        }}},
    )
    assert "new dependenc" not in text
    assert "1 CI workflow file changed" in text


def test_a_real_package_addition_is_announced():
    changes = parse_changes(_diff("requirements.txt", added=["requests==2.31.0"]))
    text = describe.at_a_glance(
        changes, {"files": ["requirements.txt"]},
        insights={"dependencies": {"status": "ok", "data": {
            "ecosystems": {"python": {}}, "totals": {"added": 2},
        }}},
    )
    assert "2 new dependencies" in text


# -- test plan -------------------------------------------------------------

def test_the_test_plan_names_the_tests_that_cover_the_changed_files():
    context = {
        "files": ["app/invoice.py"],
        "repository_tests": ["tests/test_invoice.py", "tests/test_other.py"],
    }
    steps = describe.test_plan(context)
    assert any("tests/test_invoice.py" in step for step in steps)
    assert not any("test_other" in step for step in steps)


def test_the_test_plan_names_files_nothing_covers():
    context = {"files": ["app/brand_new.py"], "repository_tests": ["tests/test_other.py"]}
    steps = describe.test_plan(context)
    assert any("No test file matches" in step and "brand_new" in step for step in steps)


def test_a_generic_stem_does_not_claim_false_coverage():
    # Every repository has an index; matching on it would invent coverage.
    context = {"files": ["src/index.ts"], "repository_tests": ["tests/index.test.ts"]}
    steps = describe.test_plan(context)
    assert not any("Run the tests that cover" in step for step in steps)


def test_the_test_plan_never_asks_for_a_pyc_file():
    context = {
        "files": ["tests/__pycache__/test_a.cpython-310.pyc"],
        "repository_tests": ["tests/test_a.py"],
    }
    steps = describe.test_plan(context, tests={"changed": [], "gaps": []})
    assert not any(".pyc" in step for step in steps)


def test_a_workflow_change_adds_a_pipeline_step():
    steps = describe.test_plan({"files": [".github/workflows/ci.yml"]})
    assert any("pipeline itself succeeds" in step for step in steps)


def test_a_docs_only_change_says_no_functional_testing_is_implied():
    steps = describe.test_plan({"files": ["README.md"]})
    assert any("no functional testing is implied" in step for step in steps)


def test_the_coverage_result_contributes_a_step():
    steps = describe.test_plan(
        {"files": ["app/a.py"], "repository_tests": []},
        coverage={"analysis": {
            "denominator": 40, "changed_line_coverage": 62.5,
            "files": [{"path": "app/a.py", "uncovered_count": 9}],
        }},
    )
    assert any("62.5%" in step and "app/a.py" in step for step in steps)


def test_configured_commands_become_steps():
    steps = describe.test_plan({"files": ["app/a.py"]}, commands=["pytest -q", "npm test"])
    assert any("`pytest -q`" in step for step in steps)


def test_no_step_says_run_the_configured_checks_for_a_directory():
    # The exact uselessness this replaced.
    steps = describe.test_plan({"files": [".github/workflows/ci.yml", "scripts/a.py"]})
    assert not any("configured checks for" in step for step in steps)


# -- user impact -----------------------------------------------------------

def test_user_impact_is_derived_from_a_real_signal():
    text = describe.user_impact({"files": ["frontend/src/App.tsx"]})
    assert "user interface changed" in text


def test_a_breaking_api_change_is_stated_first():
    text = describe.user_impact(
        {"files": ["app/api.py"]},
        insights={"api": {"status": "ok", "data": {
            "breaking_change": True,
            "routes": {"removed": ["GET /v1/invoices"]},
            "symbols": {"removed": []},
        }}},
    )
    assert text.startswith("**Breaking:**")
    assert "/v1/invoices" in text


def test_migrations_are_reported_with_a_backup_warning():
    text = describe.user_impact(
        {"files": ["db/migrate/001.sql"]},
        insights={"migrations": {"status": "ok", "data": {"migrations": [
            {"path": "db/migrate/001.sql", "risky_operations": ["drop-table"]}]}}},
    )
    assert "take a backup" in text


def test_a_ci_only_change_says_it_does_not_affect_end_users():
    text = describe.user_impact({"files": [".github/workflows/ci.yml"]})
    assert "not end users" in text


def test_no_signal_says_what_was_checked_rather_than_nothing():
    text = describe.user_impact({"files": ["scripts/internal_tool.py"]})
    assert "No user-facing impact was identified" in text
    assert "derived from the diff" in text


def test_noise_files_do_not_drive_user_impact():
    text = describe.user_impact({"files": ["app/__pycache__/views.cpython-310.pyc"]})
    assert "No user-facing impact was identified" in text


# -- risk reason -----------------------------------------------------------

def test_the_risk_reason_names_the_deciding_files():
    reason = describe.risk_reason(
        {"files": [".github/workflows/ci.yml"], "additions": 20, "deletions": 2},
        "high",
        insights={"size": {"status": "ok", "data": {
            "sensitive_areas": {"ci/cd workflows": [".github/workflows/ci.yml"]},
            "metrics": {"files_reviewable": 1, "lines_reviewable": 22},
            "substantive_concerns": ["ci"],
        }}},
    )
    assert "ci/cd workflows" in reason
    assert "`.github/workflows/ci.yml`" in reason
    assert "1 reviewable file" in reason


def test_the_risk_reason_is_never_empty():
    reason = describe.risk_reason({"files": ["a.py"], "additions": 1, "deletions": 0}, "low")
    assert reason.strip().endswith(".")
    assert "reviewable file" in reason


def test_the_risk_reason_mentions_a_spread_across_areas():
    reason = describe.risk_reason(
        {"files": ["a.py"]}, "medium",
        insights={"size": {"status": "ok", "data": {
            "substantive_concerns": ["backend", "frontend"],
            "metrics": {"files_reviewable": 2, "lines_reviewable": 30},
        }}},
    )
    assert "spans" in reason and "backend" in reason


# -- plurals ---------------------------------------------------------------

def test_plurals_read_as_prose():
    assert describe._plural("file", 1) == "file"
    assert describe._plural("file", 2) == "files"
    assert describe._plural("dependency", 2) == "dependencies"
    assert describe._plural("class", 2) == "classes"


# -- the rendered block ----------------------------------------------------

def _rendered(template=None, sections=None):
    from scripts.automation.analyze import analyse
    from scripts.common.dispatch import plan as build_plan
    from scripts.github.body import render_block

    paths = ["app/api.py", "app/__pycache__/api.cpython-310.pyc"]
    context = {
        "repository": "org/repo", "number": 1, "title": "update", "body": "",
        "head_sha": "a" * 40, "files": paths,
        "file_stats": [
            {"path": paths[0], "status": "modified", "additions": 18, "deletions": 3},
            {"path": paths[1], "status": "modified", "additions": 0, "deletions": 0},
        ],
        "commits": ["Add invoice export endpoint"], "additions": 18, "deletions": 3,
        "labels": [], "repository_tests": ["tests/test_api.py"],
        "test_inventory": {"truncated": False},
    }
    diff = _diff("app/api.py", added=["def export_invoices():", "    return []"]) + _diff(
        "app/__pycache__/api.cpython-310.pyc", binary=True
    )
    plan = dict(build_plan("all"))
    plan["policies"] = {}
    if template is not None:
        plan["metadata_options"] = {"template": template}
    meta = analyse(plan, context, diff)["metadata"]["data"]
    return meta, render_block(
        meta, context,
        sections=sections or ["at_glance", "details", "test_plan", "user_impact"],
    )


def test_at_a_glance_and_details_are_never_the_same_content():
    # The original defect: one `detail_item` template fed both sections.
    meta, block = _rendered()
    assert meta["at_glance"] != "\n".join(meta["details"])
    glance = block.split("## Details")[0]
    details = block.split("## Details")[1]
    assert "export_invoices" in details
    assert "export_invoices" not in glance


def test_the_rendered_block_never_names_a_pyc_file():
    _meta, block = _rendered()
    # The directory is named, because that is useful. The individual bytecode
    # files are not, because listing them as changes was the defect.
    assert "api.cpython-310.pyc" not in block
    assert "1 compiled Python bytecode file under `app/__pycache__`" in block


def test_the_rendered_details_name_the_added_function():
    _meta, block = _rendered()
    assert "adds function `export_invoices`" in block


def test_the_rendered_test_plan_names_the_covering_test():
    _meta, block = _rendered()
    assert "tests/test_api.py" in block


def test_an_explicit_template_still_overrides_the_derived_details():
    # Escape hatch for a fixed house style; not the default.
    _meta, block = _rendered(template={"detail_item": "Touched `{path}`."})
    assert "Touched `app/api.py`." in block
    assert "adds function" not in block.split("## Test Plan")[0].split("## Details")[1]


def test_an_empty_template_leaves_everything_derived():
    _meta, block = _rendered(template={})
    assert "adds function `export_invoices`" in block


# -- manifests and migrations ----------------------------------------------

def test_a_manifest_is_described_by_the_packages_it_moves():
    change = _change("requirements.txt", added=["django==5.0.3"],
                     removed=["django==4.2.11"])
    sentence = describe.describe_file(change)
    assert "updates `django 4.2.11 → 5.0.3`" in sentence
    # The comparison operator belongs in the file, not in prose.
    assert "==4.2.11" not in sentence


def test_a_manifest_addition_and_removal_are_named():
    change = _change("package.json", added=['    "left-pad": "1.3.0",'],
                     removed=['    "right-pad": "2.0.0",'])
    sentence = describe.describe_file(change)
    assert "adds `left-pad`" in sentence
    assert "removes `right-pad`" in sentence


def test_a_manifest_no_longer_falls_back_to_text_updated():
    change = _change("requirements.txt", added=["requests==2.31.0"])
    assert "text updated" not in describe.describe_file(change)


def test_a_migration_is_described_by_its_schema_operations():
    change = _change("app/migrations/0007_add.py", new=True, added=[
        "from django.db import migrations",
        "operations = [migrations.AddField('invoice', 'status')]",
    ])
    sentence = describe.describe_file(change)
    assert "schema operations `AddField`" in sentence


def test_a_sql_migration_operation_is_recognised():
    change = _change("db/migration/V3__add.sql", new=True,
                     added=["ALTER TABLE invoices ADD COLUMN status TEXT;",
                            "CREATE INDEX idx_status ON invoices (status);"])
    sentence = describe.describe_file(change)
    assert "create_index" in sentence or "`idx_status`" in sentence


def test_a_migration_is_not_asked_for_a_matching_unit_test():
    # A migration is verified by applying it, not by a test named after it.
    context = {
        "files": ["app/migrations/0007_add.py"],
        "repository_tests": ["tests/test_api.py"],
    }
    steps = describe.test_plan(context)
    assert not any("No test file matches" in step for step in steps)
    assert any("Apply the 1 migration" in step for step in steps)


def test_the_migration_step_asks_about_the_reverse_path():
    steps = describe.test_plan({"files": ["db/migrate/001_add.rb"]})
    assert any("reverse path" in step for step in steps)


# -- nesting ---------------------------------------------------------------

def test_grouped_bullets_render_as_a_nested_markdown_list():
    from scripts.github.body import _bullets

    changes = parse_changes(
        _diff("backend/api.py", added=["def a():"])
        + _diff("frontend/app.ts", added=["export function b() {"])
    )
    bullets = describe.detail_bullets(
        changes, {"files": ["backend/api.py", "frontend/app.ts"]}
    )
    rendered = _bullets(bullets)
    # A group heading is a top-level item; its files are indented children.
    assert "- **backend** (1 file)" in rendered
    assert "\n  - `backend/api.py`" in rendered
    # The old double-prefix defect rendered a literal dash inside the bullet.
    assert "-   - " not in rendered
