"""Read per-line hit counts out of whatever coverage format the repository emits.

Supported, because between them they cover every stack this pipeline validates:

  Cobertura XML   coverage.py ``--cov-report=xml``, Jest, .NET, PHPUnit
  LCOV            Jest/Istanbul, Vitest, c8, Go via gcov converters, Rust
  coverage.py     JSON report
  JaCoCo XML      Java and Kotlin
  Clover XML      PHPUnit
  Go coverprofile ``go test -coverprofile``
  Istanbul JSON   ``coverage-final.json``

A report is treated as untrusted input: it is produced by a test run of target
code. Every parser is bounded in bytes, tolerates a truncated or malformed file
by returning what it managed to read, and never resolves an external entity.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

#: Refuse to read a report larger than this. A real report is a few MB at most.
MAX_REPORT_BYTES = 64 * 1024 * 1024

#: ``{normalised path: {line number: hit count}}``
LineHits = dict[str, dict[int, int]]


class ReportError(Exception):
    """A coverage report could not be read. Never fatal to the caller."""


def _read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_REPORT_BYTES:
        raise ReportError(f"{path.name} is {size} bytes, over the {MAX_REPORT_BYTES} byte limit.")
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_xml(path: Path) -> ElementTree.Element:
    """Parse XML without ever resolving an external entity.

    ElementTree does not expand external entities, and a DTD-bearing document is
    rejected outright rather than parsed, which closes the entity-expansion
    avenue that a hostile report could otherwise use.
    """
    text = _read_text(path)
    if re.search(r"<!(?:DOCTYPE|ENTITY)\b", text[:8192], re.IGNORECASE):
        raise ReportError(f"{path.name} declares a DTD or entity; refusing to parse it.")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ReportError(f"{path.name} is not valid XML: {exc}") from None


def _normalise(path: str) -> str:
    """A comparable path: forward slashes, no leading ./ and no leading /."""
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _merge(into: LineHits, path: str, lines: Mapping[int, int]) -> None:
    """Merge one file's line hits, keeping the highest count per line.

    Several reports covering the same file is normal — a monorepo runs one suite
    per package — and a line covered by any suite is covered.
    """
    if not path:
        return
    target = into.setdefault(_normalise(path), {})
    for number, hits in lines.items():
        target[number] = max(target.get(number, 0), hits)


# -- Cobertura ---------------------------------------------------------------

def parse_cobertura(path: Path) -> LineHits:
    root = _parse_xml(path)
    result: LineHits = {}
    # <sources><source>/abs/prefix</source></sources> is a prefix, not a path.
    for class_element in root.iter("class"):
        filename = class_element.get("filename") or ""
        lines: dict[int, int] = {}
        for line in class_element.iter("line"):
            number = _int(line.get("number"))
            if number is None:
                continue
            lines[number] = _int(line.get("hits")) or 0
        _merge(result, filename, lines)
    # Some producers put <lines> directly under <package>/<file>.
    if not result:
        for file_element in root.iter("file"):
            filename = file_element.get("path") or file_element.get("name") or ""
            lines = {}
            for line in file_element.iter("line"):
                number = _int(line.get("number") or line.get("num"))
                if number is None:
                    continue
                lines[number] = _int(line.get("hits") or line.get("count")) or 0
            _merge(result, filename, lines)
    return result


# -- LCOV --------------------------------------------------------------------

def parse_lcov(path: Path) -> LineHits:
    result: LineHits = {}
    current = ""
    lines: dict[int, int] = {}
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current = line[3:].strip()
            lines = {}
        elif line.startswith("DA:"):
            parts = line[3:].split(",")
            number = _int(parts[0] if parts else None)
            if number is not None:
                lines[number] = _int(parts[1] if len(parts) > 1 else None) or 0
        elif line in ("end_of_record", "end_of_record;"):
            _merge(result, current, lines)
            current, lines = "", {}
    if current:
        _merge(result, current, lines)
    return result


# -- coverage.py JSON --------------------------------------------------------

def parse_coverage_json(path: Path) -> LineHits:
    try:
        payload = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{path.name} is not valid JSON: {exc.msg}") from None
    files = payload.get("files")
    if not isinstance(files, Mapping):
        raise ReportError(f"{path.name} has no 'files' mapping; not a coverage.py report.")
    result: LineHits = {}
    for filename, entry in files.items():
        if not isinstance(entry, Mapping):
            continue
        lines = {
            int(number): 1
            for number in entry.get("executed_lines") or []
            if isinstance(number, int)
        }
        lines.update({
            int(number): 0
            for number in entry.get("missing_lines") or []
            if isinstance(number, int)
        })
        _merge(result, str(filename), lines)
    return result


# -- Istanbul coverage-final.json -------------------------------------------

def parse_istanbul_json(path: Path) -> LineHits:
    try:
        payload = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{path.name} is not valid JSON: {exc.msg}") from None
    if not isinstance(payload, Mapping):
        raise ReportError(f"{path.name} is not an Istanbul report.")
    result: LineHits = {}
    for filename, entry in payload.items():
        if not isinstance(entry, Mapping):
            continue
        statements = entry.get("statementMap")
        counts = entry.get("s")
        if not isinstance(statements, Mapping) or not isinstance(counts, Mapping):
            continue
        lines: dict[int, int] = {}
        for key, location in statements.items():
            if not isinstance(location, Mapping):
                continue
            start = location.get("start")
            end = location.get("end")
            first = _int((start or {}).get("line")) if isinstance(start, Mapping) else None
            last = _int((end or {}).get("line")) if isinstance(end, Mapping) else first
            if first is None:
                continue
            hits = _int(counts.get(key)) or 0
            for number in range(first, (last or first) + 1):
                lines[number] = max(lines.get(number, 0), hits)
        _merge(result, str(entry.get("path") or filename), lines)
    return result


# -- JaCoCo ------------------------------------------------------------------

def parse_jacoco(path: Path) -> LineHits:
    root = _parse_xml(path)
    result: LineHits = {}
    for package in root.iter("package"):
        prefix = (package.get("name") or "").strip("/")
        for source in package.iter("sourcefile"):
            name = source.get("name") or ""
            lines: dict[int, int] = {}
            for line in source.iter("line"):
                number = _int(line.get("nr"))
                if number is None:
                    continue
                covered = _int(line.get("ci")) or 0
                lines[number] = covered
            full = f"{prefix}/{name}" if prefix else name
            _merge(result, full, lines)
    return result


# -- Clover ------------------------------------------------------------------

def parse_clover(path: Path) -> LineHits:
    root = _parse_xml(path)
    result: LineHits = {}
    for file_element in root.iter("file"):
        filename = file_element.get("path") or file_element.get("name") or ""
        lines: dict[int, int] = {}
        for line in file_element.iter("line"):
            if (line.get("type") or "stmt") not in ("stmt", "cond", "method"):
                continue
            number = _int(line.get("num"))
            if number is None:
                continue
            lines[number] = _int(line.get("count")) or 0
        _merge(result, filename, lines)
    return result


# -- Go coverprofile ---------------------------------------------------------

_GO_BLOCK = re.compile(
    r"\A(?P<file>.+):(?P<start_line>\d+)\.(?P<start_col>\d+),"
    r"(?P<end_line>\d+)\.(?P<end_col>\d+)\s+(?P<statements>\d+)\s+(?P<count>\d+)\s*\Z"
)


def parse_go_coverprofile(path: Path) -> LineHits:
    result: LineHits = {}
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("mode:"):
            continue
        match = _GO_BLOCK.match(line)
        if not match:
            continue
        count = _int(match.group("count")) or 0
        start = _int(match.group("start_line"))
        end = _int(match.group("end_line"))
        if start is None:
            continue
        lines = {number: count for number in range(start, (end or start) + 1)}
        # Go writes an import-path-qualified name; the tail is the repo-relative path.
        _merge(result, match.group("file"), lines)
    return result


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# -- discovery ---------------------------------------------------------------

#: ``(glob, parser, label)``. Order decides which parser a file is offered to.
FORMATS: tuple[tuple[str, Callable[[Path], LineHits], str], ...] = (
    ("**/coverage-final.json", parse_istanbul_json, "istanbul-json"),
    ("**/coverage.json", parse_coverage_json, "coverage.py-json"),
    ("**/lcov.info", parse_lcov, "lcov"),
    ("**/*.lcov", parse_lcov, "lcov"),
    ("**/jacoco*.xml", parse_jacoco, "jacoco"),
    ("**/clover*.xml", parse_clover, "clover"),
    ("**/cobertura*.xml", parse_cobertura, "cobertura"),
    ("**/coverage*.xml", parse_cobertura, "cobertura"),
    ("**/coverage.cobertura.xml", parse_cobertura, "cobertura"),
    ("**/coverage.out", parse_go_coverprofile, "go-coverprofile"),
    ("**/coverage.txt", parse_go_coverprofile, "go-coverprofile"),
    ("**/*.coverprofile", parse_go_coverprofile, "go-coverprofile"),
)

#: Directories that only ever hold third-party or build output.
SKIP_DIRECTORIES = frozenset({
    "node_modules", "vendor", ".git", ".venv", "venv", "env", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", "site-packages", "dist-info",
})

#: Never walk deeper than this looking for a report.
MAX_DEPTH = 8
#: Stop after this many candidate files, so a huge tree cannot stall the job.
MAX_CANDIDATES = 200


def discover(root: Path, patterns: Iterable[str] = ()) -> list[tuple[Path, Callable[[Path], LineHits], str]]:
    """Find coverage reports under ``root``.

    Explicit ``patterns`` from config win: a repository that knows where its
    report lands should not depend on the discovery heuristics. When no pattern
    matches anything, the built-in format globs are tried.
    """
    from ..common.globs import matches as glob_matches

    candidates: list[Path] = []
    for file_path in _walk(root):
        candidates.append(file_path)
        if len(candidates) >= MAX_CANDIDATES:
            break

    def relative(file_path: Path) -> str:
        try:
            return _normalise(str(file_path.relative_to(root)))
        except ValueError:  # pragma: no cover - defensive
            return _normalise(str(file_path))

    explicit = [p for p in patterns if p]
    found: list[tuple[Path, Callable[[Path], LineHits], str]] = []
    seen: set[Path] = set()

    if explicit:
        for file_path in candidates:
            rel = relative(file_path)
            if not any(glob_matches(pattern, rel) for pattern in explicit):
                continue
            parser, label = _parser_for(rel)
            if parser and file_path not in seen:
                seen.add(file_path)
                found.append((file_path, parser, label))
        if found:
            return found

    for pattern, parser, label in FORMATS:
        for file_path in candidates:
            rel = relative(file_path)
            if file_path in seen or not glob_matches(pattern, rel):
                continue
            seen.add(file_path)
            found.append((file_path, parser, label))
    return found


def _parser_for(relative_path: str) -> tuple[Callable[[Path], LineHits] | None, str]:
    from ..common.globs import matches as glob_matches

    for pattern, parser, label in FORMATS:
        if glob_matches(pattern, relative_path):
            return parser, label
    # An explicitly configured path with an unrecognised name: guess by suffix.
    suffix = PurePosixPath(relative_path).suffix.lower()
    if suffix == ".xml":
        return parse_cobertura, "cobertura"
    if suffix == ".json":
        return parse_coverage_json, "coverage.py-json"
    if suffix in (".info", ".lcov"):
        return parse_lcov, "lcov"
    if suffix in (".out", ".coverprofile", ".txt"):
        return parse_go_coverprofile, "go-coverprofile"
    return None, ""


def _walk(root: Path, depth: int = 0) -> Iterable[Path]:
    """Breadth-limited walk that skips dependency and build directories."""
    if depth > MAX_DEPTH:
        return
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    directories: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink():
                # A symlink out of the tree is not something to follow.
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRECTORIES and not entry.name.startswith(".git"):
                    directories.append(entry)
            elif entry.is_file():
                yield entry
        except OSError:  # pragma: no cover - races and permission errors
            continue
    for directory in directories:
        yield from _walk(directory, depth + 1)


def load(reports: Iterable[tuple[Path, Callable[[Path], LineHits], str]]
         ) -> tuple[LineHits, list[dict[str, Any]]]:
    """Parse every discovered report, recording which ones failed and why."""
    combined: LineHits = {}
    used: list[dict[str, Any]] = []
    for file_path, parser, label in reports:
        try:
            parsed = parser(file_path)
        except (ReportError, OSError) as exc:
            used.append({"path": str(file_path), "format": label, "status": "error",
                         "reason": str(exc), "files": 0})
            continue
        if not parsed:
            used.append({"path": str(file_path), "format": label, "status": "empty",
                         "reason": "No per-line data found in this report.", "files": 0})
            continue
        for path, lines in parsed.items():
            _merge(combined, path, lines)
        used.append({"path": str(file_path), "format": label, "status": "ok",
                     "reason": "", "files": len(parsed)})
    return combined, used
