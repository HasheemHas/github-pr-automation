"""Glob-to-regex conversion for path matching.

Everything outside the recognised glob tokens is escaped with ``re.escape``, so
paths and patterns containing regex metacharacters (``[``, ``]``, ``(``, ``)``,
``+``, ``.``, ``\\``) are matched literally.

Supported tokens:
  ``**/``  zero or more leading directories (so ``**/Dockerfile`` also matches
           a root-level ``Dockerfile``)
  ``**``   any number of characters, including ``/``
  ``*``    any number of characters except ``/``
  ``?``    exactly one character except ``/``
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, Sequence


def _translate(pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            # Zero or more path segments, so the pattern also matches at root.
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern into an anchored regex."""
    return re.compile(f"(?s:{_translate(pattern)})\\Z")


def matches(pattern: str, path: str) -> bool:
    """True when ``path`` matches ``pattern``. Leading ``./`` is ignored."""
    normalised = path[2:] if path.startswith("./") else path
    return glob_to_regex(pattern).match(normalised) is not None


def any_match(patterns: Iterable[str], paths: Iterable[str]) -> bool:
    """True when any path matches any pattern."""
    path_list: Sequence[str] = list(paths)
    return any(matches(pattern, path) for pattern in patterns for path in path_list)


def matching_paths(patterns: Iterable[str], paths: Iterable[str]) -> list[str]:
    """Every path matching at least one pattern, order preserved."""
    pattern_list = list(patterns)
    return [p for p in paths if any(matches(pattern, p) for pattern in pattern_list)]
