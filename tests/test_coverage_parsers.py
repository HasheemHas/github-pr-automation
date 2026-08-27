"""Coverage report parsing across every supported format."""
from __future__ import annotations

import json

import pytest

from scripts.coverage import parsers

COBERTURA = """<?xml version="1.0" ?>
<coverage>
  <sources><source>/build/src</source></sources>
  <packages>
    <package name="app">
      <classes>
        <class filename="app/main.py" name="main">
          <lines>
            <line number="10" hits="3"/>
            <line number="11" hits="0"/>
            <line number="12" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""

LCOV = """TN:
SF:src/api.ts
DA:4,2
DA:5,0
DA:6,7
end_of_record
SF:src/util.ts
DA:1,1
end_of_record
"""

JACOCO = """<?xml version="1.0" ?>
<report>
  <package name="com/example/app">
    <sourcefile name="Service.java">
      <line nr="20" ci="4" mi="0"/>
      <line nr="21" ci="0" mi="2"/>
    </sourcefile>
  </package>
</report>
"""

CLOVER = """<?xml version="1.0" ?>
<coverage>
  <project>
    <file name="src/Service.php">
      <line num="15" type="stmt" count="3"/>
      <line num="16" type="stmt" count="0"/>
      <line num="17" type="method" count="1"/>
    </file>
  </project>
</coverage>
"""

GO_PROFILE = """mode: set
github.com/org/repo/handler.go:10.20,14.3 3 1
github.com/org/repo/handler.go:20.2,21.10 1 0
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# -- per-format parsing ----------------------------------------------------

def test_cobertura(tmp_path):
    hits = parsers.parse_cobertura(_write(tmp_path, "coverage.xml", COBERTURA))
    assert hits == {"app/main.py": {10: 3, 11: 0, 12: 1}}


def test_lcov(tmp_path):
    hits = parsers.parse_lcov(_write(tmp_path, "lcov.info", LCOV))
    assert hits == {"src/api.ts": {4: 2, 5: 0, 6: 7}, "src/util.ts": {1: 1}}


def test_coverage_py_json(tmp_path):
    payload = {"files": {"app/main.py": {
        "executed_lines": [10, 12], "missing_lines": [11],
    }}}
    hits = parsers.parse_coverage_json(
        _write(tmp_path, "coverage.json", json.dumps(payload))
    )
    assert hits == {"app/main.py": {10: 1, 12: 1, 11: 0}}


def test_istanbul_json(tmp_path):
    payload = {"/build/src/api.ts": {
        "path": "src/api.ts",
        "statementMap": {"0": {"start": {"line": 4}, "end": {"line": 4}},
                         "1": {"start": {"line": 5}, "end": {"line": 6}}},
        "s": {"0": 3, "1": 0},
    }}
    hits = parsers.parse_istanbul_json(
        _write(tmp_path, "coverage-final.json", json.dumps(payload))
    )
    assert hits == {"src/api.ts": {4: 3, 5: 0, 6: 0}}


def test_jacoco(tmp_path):
    hits = parsers.parse_jacoco(_write(tmp_path, "jacoco.xml", JACOCO))
    assert hits == {"com/example/app/Service.java": {20: 4, 21: 0}}


def test_clover(tmp_path):
    hits = parsers.parse_clover(_write(tmp_path, "clover.xml", CLOVER))
    assert hits == {"src/Service.php": {15: 3, 16: 0, 17: 1}}


def test_go_coverprofile_expands_block_ranges(tmp_path):
    hits = parsers.parse_go_coverprofile(_write(tmp_path, "coverage.out", GO_PROFILE))
    covered = hits["github.com/org/repo/handler.go"]
    assert covered[10] == 1 and covered[14] == 1
    assert covered[20] == 0 and covered[21] == 0


# -- robustness ------------------------------------------------------------

def test_a_dtd_bearing_xml_report_is_refused(tmp_path):
    # Entity expansion is the one attack an XML report could attempt.
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><coverage/>'
    with pytest.raises(parsers.ReportError, match="DTD or entity"):
        parsers.parse_cobertura(_write(tmp_path, "coverage.xml", evil))


def test_malformed_xml_raises_a_report_error_not_a_parse_error(tmp_path):
    with pytest.raises(parsers.ReportError, match="not valid XML"):
        parsers.parse_cobertura(_write(tmp_path, "coverage.xml", "<coverage>"))


def test_malformed_json_raises_a_report_error(tmp_path):
    with pytest.raises(parsers.ReportError, match="not valid JSON"):
        parsers.parse_coverage_json(_write(tmp_path, "coverage.json", "{oops"))


def test_json_without_a_files_mapping_is_rejected(tmp_path):
    with pytest.raises(parsers.ReportError, match="not a coverage.py report"):
        parsers.parse_coverage_json(_write(tmp_path, "coverage.json", '{"totals": {}}'))


def test_an_oversized_report_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "MAX_REPORT_BYTES", 10)
    with pytest.raises(parsers.ReportError, match="over the"):
        parsers.parse_lcov(_write(tmp_path, "lcov.info", LCOV))


def test_paths_are_normalised(tmp_path):
    content = "SF:./src/api.ts\nDA:1,1\nend_of_record\n"
    hits = parsers.parse_lcov(_write(tmp_path, "lcov.info", content))
    assert list(hits) == ["src/api.ts"]


# -- discovery -------------------------------------------------------------

def test_discovery_finds_reports_and_identifies_their_format(tmp_path):
    _write(tmp_path, "coverage.xml", COBERTURA)
    _write(tmp_path, "frontend/coverage/lcov.info", LCOV)
    found = parsers.discover(tmp_path)
    formats = {label for _path, _parser, label in found}
    assert {"cobertura", "lcov"} <= formats


def test_discovery_skips_dependency_directories(tmp_path):
    _write(tmp_path, "node_modules/pkg/coverage.xml", COBERTURA)
    assert parsers.discover(tmp_path) == []


def test_explicit_patterns_win_over_the_heuristics(tmp_path):
    _write(tmp_path, "coverage.xml", COBERTURA)
    _write(tmp_path, "reports/mine.xml", COBERTURA)
    found = parsers.discover(tmp_path, ["reports/mine.xml"])
    assert [p.name for p, _parser, _label in found] == ["mine.xml"]


def test_an_explicit_pattern_matching_nothing_falls_back_to_discovery(tmp_path):
    _write(tmp_path, "coverage.xml", COBERTURA)
    found = parsers.discover(tmp_path, ["nowhere/here.xml"])
    assert [p.name for p, _parser, _label in found] == ["coverage.xml"]


def test_load_merges_reports_and_records_failures(tmp_path):
    good = _write(tmp_path, "coverage.xml", COBERTURA)
    bad = _write(tmp_path, "broken.xml", "<coverage>")
    hits, used = parsers.load([
        (good, parsers.parse_cobertura, "cobertura"),
        (bad, parsers.parse_cobertura, "cobertura"),
    ])
    assert hits == {"app/main.py": {10: 3, 11: 0, 12: 1}}
    statuses = {entry["status"] for entry in used}
    assert statuses == {"ok", "error"}


def test_load_keeps_the_highest_hit_count_when_reports_overlap(tmp_path):
    first = _write(tmp_path, "a/coverage.xml", COBERTURA)
    second = _write(tmp_path, "b/coverage.xml", COBERTURA.replace('hits="0"', 'hits="5"'))
    hits, _used = parsers.load([
        (first, parsers.parse_cobertura, "cobertura"),
        (second, parsers.parse_cobertura, "cobertura"),
    ])
    # Line 11 is uncovered in one suite and covered in the other: covered wins.
    assert hits["app/main.py"][11] == 5


def test_an_empty_report_is_recorded_as_empty_not_as_a_failure(tmp_path):
    empty = _write(tmp_path, "coverage.xml", '<?xml version="1.0"?><coverage/>')
    hits, used = parsers.load([(empty, parsers.parse_cobertura, "cobertura")])
    assert hits == {}
    assert used[0]["status"] == "empty"
