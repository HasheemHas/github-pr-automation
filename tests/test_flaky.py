"""JUnit parsing and flaky-vs-broken classification."""
from __future__ import annotations

from scripts.flaky import junit
from scripts.flaky.run import classify, clear_reports, commands_to_repeat

PASSING = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="pytest" tests="3">
    <testcase classname="tests.test_a" name="test_one" time="0.1"/>
    <testcase classname="tests.test_a" name="test_two" time="0.2"/>
    <testcase classname="tests.test_b" name="test_three" time="0.3"/>
  </testsuite>
</testsuites>
"""

ONE_FAILING = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="pytest" tests="3">
    <testcase classname="tests.test_a" name="test_one" time="0.1"/>
    <testcase classname="tests.test_a" name="test_two" time="0.2">
      <failure message="AssertionError: timing">assert 0.4 &lt; 0.3</failure>
    </testcase>
    <testcase classname="tests.test_b" name="test_three" time="0.3"/>
  </testsuite>
</testsuites>
"""

WITH_RERUN = """<?xml version="1.0"?>
<testsuite name="surefire" tests="1">
  <testcase classname="com.example.ServiceTest" name="testRetry" time="1.0">
    <flakyFailure message="Connection reset">retried once</flakyFailure>
  </testcase>
</testsuite>
"""

WITH_SKIP_AND_ERROR = """<?xml version="1.0"?>
<testsuite name="pytest" tests="2">
  <testcase classname="tests.test_c" name="test_skipped">
    <skipped message="needs a database"/>
  </testcase>
  <testcase classname="tests.test_c" name="test_errored">
    <error message="fixture failure">setup blew up</error>
  </testcase>
</testsuite>
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# -- parsing ---------------------------------------------------------------

def test_passing_cases_are_read(tmp_path):
    suite = junit.parse_file(_write(tmp_path, "junit.xml", PASSING))
    assert suite.counts["passed"] == 3
    assert suite.cases[0].identifier == "tests.test_a::test_one"
    assert suite.cases[0].time == 0.1


def test_a_failure_is_read_with_its_message(tmp_path):
    suite = junit.parse_file(_write(tmp_path, "junit.xml", ONE_FAILING))
    failing = next(case for case in suite.cases if case.status == junit.FAILED)
    assert failing.identifier == "tests.test_a::test_two"
    assert "timing" in failing.message


def test_skips_and_errors_are_distinguished(tmp_path):
    suite = junit.parse_file(_write(tmp_path, "junit.xml", WITH_SKIP_AND_ERROR))
    statuses = {case.name: case.status for case in suite.cases}
    assert statuses == {"test_skipped": junit.SKIPPED, "test_errored": junit.ERROR}


def test_a_framework_rerun_marker_is_read(tmp_path):
    suite = junit.parse_file(_write(tmp_path, "surefire.xml", WITH_RERUN))
    assert suite.cases[0].reran is True


def test_parameterised_names_are_kept_verbatim():
    assert junit.identify("tests.test_a", "test_add[2-3]") == "tests.test_a::test_add[2-3]"


def test_a_case_without_a_classname_falls_back_to_the_file():
    assert junit.identify("", "test_one", file="tests/test_a.py") == "tests/test_a.py::test_one"


def test_a_malformed_file_yields_an_error_not_an_exception(tmp_path):
    suite = junit.parse_file(_write(tmp_path, "junit.xml", "<testsuite>"))
    assert suite.cases == []
    assert "not valid XML" in suite.errors[0]


def test_a_dtd_bearing_report_is_refused(tmp_path):
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><testsuite/>'
    suite = junit.parse_file(_write(tmp_path, "junit.xml", evil))
    assert "DTD or entity" in suite.errors[0]


def test_a_non_junit_xml_file_is_ignored_quietly(tmp_path):
    suite = junit.parse_file(_write(tmp_path, "coverage.xml", "<coverage><lines/></coverage>"))
    assert suite.cases == [] and suite.errors == []


def test_an_unreadable_file_is_reported(tmp_path):
    suite = junit.parse_file(tmp_path / "missing.xml")
    assert "Could not read" in suite.errors[0]


def test_discovery_finds_reports_in_the_usual_places(tmp_path):
    _write(tmp_path, "junit.xml", PASSING)
    _write(tmp_path, "target/surefire-reports/TEST-Service.xml", WITH_RERUN)
    _write(tmp_path, "node_modules/pkg/junit.xml", PASSING)
    found = {path.name for path in junit.discover(tmp_path)}
    assert found == {"junit.xml", "TEST-Service.xml"}


def test_load_collects_cases_across_reports(tmp_path):
    paths = [
        _write(tmp_path, "a/junit.xml", PASSING),
        _write(tmp_path, "b/junit.xml", WITH_RERUN),
    ]
    cases, errors = junit.load(paths)
    assert len(cases) == 4
    assert errors == []


# -- classification --------------------------------------------------------

def _case(identifier, status, *, reran=False):
    classname, _, name = identifier.partition("::")
    return junit.TestCase(
        identifier=identifier, name=name, classname=classname, status=status, reran=reran,
    )


def test_a_test_that_passes_and_fails_is_flaky():
    result = classify([
        [_case("t::a", junit.PASSED), _case("t::b", junit.PASSED)],
        [_case("t::a", junit.FAILED), _case("t::b", junit.PASSED)],
    ])
    assert [entry["identifier"] for entry in result["flaky"]] == ["t::a"]
    assert result["consistent_failures"] == []
    assert "passed 1 time(s) and failed 1 time(s)" in result["flaky"][0]["reason"]


def test_a_test_that_always_fails_is_a_real_failure_not_a_flake():
    result = classify([
        [_case("t::a", junit.FAILED)],
        [_case("t::a", junit.FAILED)],
    ])
    assert result["flaky"] == []
    assert [entry["identifier"] for entry in result["consistent_failures"]] == ["t::a"]


def test_an_error_counts_the_same_as_a_failure():
    result = classify([
        [_case("t::a", junit.PASSED)],
        [_case("t::a", junit.ERROR)],
    ])
    assert [entry["identifier"] for entry in result["flaky"]] == ["t::a"]


def test_a_framework_rerun_marker_makes_a_test_flaky_from_a_single_run():
    result = classify([[_case("t::a", junit.PASSED, reran=True)]])
    assert [entry["identifier"] for entry in result["flaky"]] == ["t::a"]
    assert "recorded a retry" in result["flaky"][0]["reason"]


def test_a_stable_suite_reports_nothing():
    result = classify([
        [_case("t::a", junit.PASSED)],
        [_case("t::a", junit.PASSED)],
    ])
    assert result["flaky"] == [] and result["consistent_failures"] == []
    assert result["total_tests"] == 1


def test_a_test_collected_in_only_some_runs_is_reported_separately():
    result = classify([
        [_case("t::a", junit.PASSED), _case("t::b", junit.PASSED)],
        [_case("t::a", junit.PASSED)],
    ])
    assert [e["identifier"] for e in result["inconsistent_collection"]] == ["t::b"]
    assert result["flaky"] == []


def test_a_skipped_test_is_not_inconsistent_collection():
    result = classify([
        [_case("t::a", junit.PASSED), _case("t::b", junit.SKIPPED)],
        [_case("t::a", junit.PASSED), _case("t::b", junit.SKIPPED)],
    ])
    assert result["inconsistent_collection"] == []


def test_the_minimum_failure_count_is_configurable():
    runs = [
        [_case("t::a", junit.PASSED)],
        [_case("t::a", junit.FAILED)],
        [_case("t::a", junit.PASSED)],
    ]
    assert classify(runs, min_failures=1)["flaky"]
    # Requiring two failures reclassifies a single blip as stable.
    assert classify(runs, min_failures=2)["flaky"] == []


def test_flaky_tests_are_ordered_by_failure_count():
    result = classify([
        [_case("t::a", junit.PASSED), _case("t::b", junit.PASSED)],
        [_case("t::a", junit.FAILED), _case("t::b", junit.FAILED)],
        [_case("t::a", junit.FAILED), _case("t::b", junit.PASSED)],
    ])
    assert [entry["identifier"] for entry in result["flaky"]] == ["t::a", "t::b"]


def test_failure_messages_are_collected_without_duplicates():
    first = junit.TestCase("t::a", "a", "t", junit.FAILED, message="boom")
    second = junit.TestCase("t::a", "a", "t", junit.FAILED, message="boom")
    result = classify([[first], [second]])
    assert result["consistent_failures"][0]["messages"] == ["boom"]


# -- the runner's helpers --------------------------------------------------

def test_stale_reports_are_cleared_between_runs(tmp_path):
    paths = [_write(tmp_path, "junit.xml", PASSING)]
    assert clear_reports(paths) == 1
    assert not paths[0].exists()
    # Clearing an already-missing file is not an error.
    assert clear_reports(paths) == 0


def test_an_explicit_flaky_command_overrides_the_test_group():
    from scripts.common.config import Target

    target = Target(owner="org", repo="repo", config={
        "commands": {"test": ["pytest -q"]},
    })
    assert [c.run for c in commands_to_repeat(target, None, "pytest -q --count=2")] == [
        "pytest -q --count=2"
    ]
    assert [c.run for c in commands_to_repeat(target, None, "")] == ["pytest -q"]
