"""Read per-test outcomes from JUnit XML.

Every test runner this pipeline meets can emit JUnit XML — pytest, Jest, Vitest,
PHPUnit, Go via gotestsum, Maven Surefire, .NET via trx2junit — which makes it
the one format worth supporting properly.

Two things are extracted:

  * the outcome of each test case, keyed stably so the same test can be compared
    across runs
  * the framework's own rerun markers (``flakyFailure``, ``flakyError``,
    ``rerunFailure``, ``rerunError``), which prove flakiness from a single run

Like the coverage parsers, this treats the file as untrusted output from a test
run: bounded in size, no DTD, and a malformed file yields what could be read
rather than an exception.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

MAX_REPORT_BYTES = 64 * 1024 * 1024

#: Report locations the common runners write to, relative to the repository root.
DEFAULT_PATTERNS = (
    "**/junit*.xml",
    "**/*junit.xml",
    "**/TEST-*.xml",
    "**/test-results/**/*.xml",
    "**/test-report*.xml",
    "**/reports/**/*.xml",
    "**/surefire-reports/*.xml",
    "**/failsafe-reports/*.xml",
    "**/build/test-results/**/*.xml",
    "**/target/surefire-reports/*.xml",
)

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"


@dataclass(frozen=True)
class TestCase:
    """One test case outcome from one report."""

    identifier: str
    name: str
    classname: str
    status: str
    time: float = 0.0
    message: str = ""
    #: The framework itself recorded a retry for this test, so it is flaky by
    #: the framework's own definition.
    reran: bool = False
    file: str = ""


@dataclass
class Suite:
    """Everything read out of one JUnit XML file."""

    path: str = ""
    cases: list[TestCase] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for case in self.cases if case.status == status)
            for status in (PASSED, FAILED, ERROR, SKIPPED)
        }


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def identify(classname: str, name: str, *, file: str = "") -> str:
    """A stable identifier for one test across runs.

    Parameterised test names are kept verbatim: ``test_add[2-3]`` failing while
    ``test_add[4-5]`` passes is exactly the kind of detail that gets lost when
    parameters are stripped, and it is usually the clue to the cause.
    """
    left = _clean(classname) or _clean(file)
    right = _clean(name)
    return f"{left}::{right}" if left else right


def parse_file(path: Path) -> Suite:
    """Read one JUnit XML file. Never raises for a bad file."""
    suite = Suite(path=str(path))
    try:
        size = path.stat().st_size
        if size > MAX_REPORT_BYTES:
            suite.errors.append(f"{path.name} is {size} bytes; over the size limit.")
            return suite
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        suite.errors.append(f"Could not read {path.name}: {exc}")
        return suite

    if re.search(r"<!(?:DOCTYPE|ENTITY)\b", text[:8192], re.IGNORECASE):
        suite.errors.append(f"{path.name} declares a DTD or entity; refusing to parse it.")
        return suite
    if "<testsuite" not in text and "<testcase" not in text:
        # Not a JUnit report at all — a coverage XML in the same directory, say.
        return suite

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        suite.errors.append(f"{path.name} is not valid XML: {exc}")
        return suite

    for case in _iter_cases(root):
        suite.cases.append(case)
    return suite


def _iter_cases(root: ElementTree.Element) -> Iterator[TestCase]:
    for element in root.iter("testcase"):
        name = element.get("name") or ""
        classname = element.get("classname") or element.get("class") or ""
        file_attr = element.get("file") or ""
        status = PASSED
        message = ""
        reran = False

        for child in element:
            tag = child.tag.split("}")[-1]
            if tag == "failure":
                status = FAILED
                message = message or _message(child)
            elif tag == "error":
                status = ERROR
                message = message or _message(child)
            elif tag == "skipped":
                if status == PASSED:
                    status = SKIPPED
                    message = message or _message(child)
            elif tag in ("flakyFailure", "flakyError", "rerunFailure", "rerunError"):
                # The runner retried this test. Surefire records a retry that
                # eventually passed as flakyFailure; pytest-rerunfailures and
                # Jest write rerunFailure.
                reran = True
                message = message or _message(child)

        # An explicit status attribute wins where a runner sets one.
        explicit = (element.get("status") or "").strip().lower()
        if explicit in ("pass", "passed", "success"):
            status = PASSED if status == PASSED else status
        elif explicit in ("fail", "failed"):
            status = FAILED
        elif explicit in ("skipped", "notrun", "disabled"):
            status = SKIPPED if status == PASSED else status

        yield TestCase(
            identifier=identify(classname, name, file=file_attr),
            name=_clean(name),
            classname=_clean(classname),
            status=status,
            time=_float(element.get("time")),
            message=message[:400],
            reran=reran,
            file=file_attr,
        )


def _message(element: ElementTree.Element) -> str:
    parts = [element.get("message") or "", element.get("type") or "", (element.text or "")]
    return _clean(" ".join(part for part in parts if part))[:400]


def _float(value: str | None) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def discover(root: Path, patterns: Iterable[str] = DEFAULT_PATTERNS) -> list[Path]:
    """Find JUnit XML reports under ``root``, skipping dependency directories."""
    from ..common.globs import matches
    from ..coverage.parsers import _walk

    globs = [p for p in patterns if p] or list(DEFAULT_PATTERNS)
    found: list[Path] = []
    for file_path in _walk(root):
        if file_path.suffix.lower() != ".xml":
            continue
        try:
            relative = str(file_path.relative_to(root)).replace("\\", "/")
        except ValueError:  # pragma: no cover - defensive
            relative = str(file_path)
        if any(matches(pattern, relative) for pattern in globs):
            found.append(file_path)
        if len(found) >= 500:
            break
    return sorted(found)


def load(paths: Iterable[Path]) -> tuple[list[TestCase], list[str]]:
    """Parse every report, returning the cases and any read errors."""
    cases: list[TestCase] = []
    errors: list[str] = []
    for path in paths:
        suite = parse_file(path)
        cases.extend(suite.cases)
        errors.extend(suite.errors)
    return cases, errors
