"""Database migration checks: framework detection, rollback, risky operations."""
from __future__ import annotations

from scripts.insights.changes import parse_changes
from scripts.insights.migrations import detect_framework, evaluate, risky_operations


def _new_file(path, lines):
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _modified(path, removed, added):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"
    )


def _deleted(path, lines):
    body = "".join(f"-{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
        f"--- a/{path}\n+++ /dev/null\n@@ -1,{len(lines)} +0,0 @@\n{body}"
    )


def _evaluate(diff, paths=None, config=None):
    changes = parse_changes(diff)
    context = {"files": list(paths) if paths is not None else sorted(changes)}
    return evaluate(changes, context, config or {})


def _rules(result):
    return {f["rule"] for f in result["findings"]}


REVERSIBLE_DJANGO = [
    "from django.db import migrations, models",
    "",
    "class Migration(migrations.Migration):",
    "    dependencies = [('app', '0041_previous')]",
    "    operations = [",
    "        migrations.AddField('invoice', 'status', models.CharField(max_length=20,"
    " null=True)),",
    "    ]",
]


# -- framework detection ---------------------------------------------------

def test_frameworks_are_detected_from_the_path():
    assert detect_framework("app/migrations/0042_add_status.py") == "django"
    assert detect_framework("alembic/versions/3f8a1c2b_add.py") == "alembic"
    assert detect_framework("database/migrations/2024_01_02_120000_add.php") == "laravel"
    assert detect_framework("db/migrate/20240102120000_add_status.rb") == "rails"
    assert detect_framework("src/main/resources/db/migration/V12__add.sql") == "flyway"
    assert detect_framework("prisma/migrations/20240102_init/migration.sql") == "prisma"
    assert detect_framework("migrations/000123_add_status.up.sql") == "golang-migrate"
    assert detect_framework("app/models.py") is None


def test_a_migrations_package_init_is_not_a_migration():
    result = _evaluate(_new_file("app/migrations/__init__.py", [""]))
    assert result["migrations"] == []


def test_no_migration_and_no_model_change_passes_quietly():
    result = _evaluate(_new_file("app/views.py", ["def index(): pass"]))
    assert result["status"] == "pass"
    assert result["findings"] == []


# -- naming ----------------------------------------------------------------

def test_an_unnumbered_python_file_under_migrations_is_not_treated_as_one():
    # The numeric prefix is what makes a Django migration identifiable. A helper
    # module living in the same package is not a migration, and guessing that it
    # is would produce a naming complaint about a file that needs none.
    result = _evaluate(_new_file("app/migrations/helpers.py", ["def noop(): pass"]))
    assert result["migrations"] == []
    assert "migration-naming" not in _rules(result)


def test_a_conforming_name_is_accepted():
    result = _evaluate(_new_file("app/migrations/0042_add_status.py", REVERSIBLE_DJANGO))
    record = result["migrations"][0]
    assert record["naming_ok"] is True
    assert "migration-naming" not in _rules(result)


def test_a_flyway_file_must_follow_the_version_convention():
    good = _evaluate(_new_file("db/migration/V12__add_status.sql", ["ALTER TABLE x ADD y INT;"]))
    bad = _evaluate(_new_file("db/migration/add_status.sql", ["ALTER TABLE x ADD y INT;"]))
    assert "migration-naming" not in _rules(good)
    assert "migration-naming" in _rules(bad)


def test_naming_checks_can_be_disabled():
    result = _evaluate(
        _new_file("db/migration/add_status.sql", ["ALTER TABLE x ADD y INT;"]),
        config={"migration_policy": {"require_naming": False}},
    )
    assert "migration-naming" not in _rules(result)


# -- reversibility ---------------------------------------------------------

def test_a_migration_with_no_reverse_path_is_reported():
    result = _evaluate(_new_file("alembic/versions/3f8a1c2b9d40_add_status.py", [
        "def upgrade():", "    op.add_column('invoice', sa.Column('status', sa.String()))",
    ]))
    assert "migration-no-rollback" in _rules(result)


def test_a_downgrade_function_satisfies_the_rollback_requirement():
    result = _evaluate(_new_file("alembic/versions/3f8a1c2b9d40_add_status.py", [
        "def upgrade():", "    op.add_column('invoice', sa.Column('status', sa.String()))",
        "def downgrade():", "    op.drop_column('invoice', 'status')",
    ]))
    assert "migration-no-rollback" not in _rules(result)
    assert result["migrations"][0]["reversible"] is True


def test_declaring_a_migration_irreversible_satisfies_the_requirement():
    result = _evaluate(_new_file("app/migrations/0042_backfill.py", [
        "# This migration is irreversible: the source column is dropped.",
        "operations = [migrations.RunPython(forwards)]",
    ]))
    assert "migration-no-rollback" not in _rules(result)
    assert result["migrations"][0]["declared_irreversible"] is True


def test_a_laravel_down_method_is_recognised():
    result = _evaluate(_new_file("database/migrations/2024_01_02_120000_add.php", [
        "public function up() { Schema::table('x', fn($t) => $t->string('y')); }",
        "public function down() { Schema::table('x', fn($t) => $t->dropColumn('y')); }",
    ]))
    assert "migration-no-rollback" not in _rules(result)


def test_golang_migrate_finds_its_down_file_as_a_sibling():
    diff = (
        _new_file("migrations/000123_add_status.up.sql", ["ALTER TABLE x ADD y INT;"])
        + _new_file("migrations/000123_add_status.down.sql", ["ALTER TABLE x DROP COLUMN y;"])
    )
    result = _evaluate(diff)
    assert "migration-no-rollback" not in _rules(result)


def test_flyway_is_not_asked_for_a_rollback():
    # Flyway's model is forward-only; demanding a down migration is wrong.
    result = _evaluate(_new_file("db/migration/V12__add.sql", ["ALTER TABLE x ADD y INT;"]))
    assert "migration-no-rollback" not in _rules(result)


def test_the_rollback_requirement_can_be_disabled():
    result = _evaluate(
        _new_file("alembic/versions/3f8a1c2b9d40_add.py", ["def upgrade(): pass"]),
        config={"migration_policy": {"require_rollback": False}},
    )
    assert "migration-no-rollback" not in _rules(result)


# -- risky operations ------------------------------------------------------

def test_risky_operations_are_recognised():
    found = {op["rule"] for op in risky_operations(
        "DROP TABLE invoices;\nTRUNCATE users;\nALTER TABLE x ADD COLUMN y INT NOT NULL;"
    )}
    assert {"drop-table", "truncate", "not-null-without-default"} <= found


def test_dropping_a_table_is_critical():
    result = _evaluate(_new_file("db/migration/V12__drop.sql", ["DROP TABLE invoices;"]))
    finding = next(f for f in result["findings"] if f["rule"] == "migration-drop-table")
    assert finding["severity"] == "critical"
    assert "two steps" in finding["detail"] or "later release" in finding["detail"]


def test_dropping_a_column_via_an_orm_is_detected():
    result = _evaluate(_new_file("app/migrations/0042_drop.py", [
        "operations = [migrations.RemoveField('invoice', 'legacy_status')]",
    ]))
    assert "migration-drop-column" in _rules(result)


def test_a_non_concurrent_index_build_is_reported():
    result = _evaluate(_new_file("db/migration/V12__index.sql", [
        "CREATE INDEX idx_invoice_status ON invoices (status);",
    ]))
    assert "migration-create-index-blocking" in _rules(result)


def test_a_concurrent_index_build_is_not_reported():
    result = _evaluate(_new_file("db/migration/V12__index.sql", [
        "CREATE INDEX CONCURRENTLY idx_invoice_status ON invoices (status);",
    ]))
    assert "migration-create-index-blocking" not in _rules(result)


def test_an_update_with_a_where_clause_is_not_reported():
    with_where = _evaluate(_new_file("db/migration/V12__fix.sql", [
        "UPDATE invoices SET status = 'draft' WHERE status IS NULL;",
    ]))
    without = _evaluate(_new_file("db/migration/V13__fix.sql", [
        "UPDATE invoices SET status = 'draft';",
    ]))
    assert "migration-update-without-where" not in _rules(with_where)
    assert "migration-update-without-where" in _rules(without)


def test_a_rename_is_reported_as_breaking_running_instances():
    result = _evaluate(_new_file("db/migration/V12__rename.sql", [
        "ALTER TABLE invoices RENAME COLUMN status TO state;",
    ]))
    finding = next(f for f in result["findings"] if f["rule"] == "migration-rename")
    assert "previous release" in finding["detail"]


# -- edited and deleted migrations -----------------------------------------

def test_editing_an_existing_migration_is_high_severity():
    result = _evaluate(_modified(
        "db/migration/V12__add.sql",
        removed=["ALTER TABLE x ADD y INT;"],
        added=["ALTER TABLE x ADD y BIGINT;"],
    ))
    finding = next(f for f in result["findings"] if f["rule"] == "migration-edited-in-place")
    assert finding["severity"] == "high"
    assert result["migrations"][0]["edited_in_place"] is True


def test_editing_an_applied_migration_can_be_allowed():
    result = _evaluate(
        _modified("db/migration/V12__add.sql", removed=["ALTER TABLE x ADD y INT;"],
                  added=["ALTER TABLE x ADD y BIGINT;"]),
        config={"migration_policy": {"allow_editing_applied": True}},
    )
    assert "migration-edited-in-place" not in _rules(result)


def test_deleting_a_migration_is_reported():
    result = _evaluate(_deleted("db/migration/V12__add.sql", ["ALTER TABLE x ADD y INT;"]))
    assert "migration-deleted" in _rules(result)


# -- cross-file checks -----------------------------------------------------

def test_a_model_change_with_no_migration_is_reported():
    result = _evaluate(_modified("app/models.py", removed=["status = None"],
                                added=["status = models.CharField(max_length=20)"]))
    assert "migration-missing-for-model-change" in _rules(result)


def test_a_model_change_with_a_migration_is_not_reported():
    diff = (
        _modified("app/models.py", removed=["x = 1"], added=["x = 2"])
        + _new_file("app/migrations/0042_add.py", REVERSIBLE_DJANGO)
    )
    result = _evaluate(diff)
    assert "migration-missing-for-model-change" not in _rules(result)


def test_a_migration_with_no_code_change_is_a_low_note():
    result = _evaluate(_new_file("app/migrations/0042_add.py", REVERSIBLE_DJANGO))
    finding = next(f for f in result["findings"] if f["rule"] == "migration-without-code")
    assert finding["severity"] == "low"


def test_too_many_migrations_in_one_pr_can_be_limited():
    diff = "".join(
        _new_file(f"app/migrations/004{n}_add.py", REVERSIBLE_DJANGO) for n in range(1, 4)
    )
    assert "migration-too-many" not in _rules(_evaluate(diff))
    limited = _evaluate(diff, config={"migration_policy": {"max_per_pr": 2}})
    assert "migration-too-many" in _rules(limited)


def test_mixed_frameworks_are_noted():
    diff = (
        _new_file("app/migrations/0042_add.py", REVERSIBLE_DJANGO)
        + _new_file("db/migrate/20240102120000_add.rb", ["def change; end"])
    )
    assert "migration-mixed-frameworks" in _rules(_evaluate(diff))


# -- the envelope ----------------------------------------------------------

def test_migration_checks_are_advisory_by_default():
    result = _evaluate(_new_file("db/migration/V12__drop.sql", ["DROP TABLE invoices;"]))
    assert result["status"] == "warn"


def test_failing_can_be_switched_on():
    result = _evaluate(
        _new_file("db/migration/V12__drop.sql", ["DROP TABLE invoices;"]),
        config={"migration_policy": {"fail_on": "critical"}},
    )
    assert result["status"] == "fail"


def test_the_check_can_be_disabled():
    result = _evaluate(
        _new_file("db/migration/V12__drop.sql", ["DROP TABLE invoices;"]),
        config={"migration_policy": {"enabled": False}},
    )
    assert result["status"] == "not_run"


def test_the_record_reports_what_was_found_per_file():
    result = _evaluate(_new_file("db/migration/V12__drop.sql", ["DROP TABLE invoices;"]))
    record = result["migrations"][0]
    assert record["framework"] == "flyway"
    assert record["status"] == "added"
    assert "drop-table" in record["risky_operations"]
