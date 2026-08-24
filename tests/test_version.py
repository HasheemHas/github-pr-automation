"""Semantic version ordering. Tag API order is never trusted."""
from __future__ import annotations

import pytest

from scripts.common.errors import AutomationError
from scripts.release.version import (
    bump,
    format_version,
    latest_version,
    next_version,
    parse_tag,
    sorted_versions,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("  v0.0.1  ", (0, 0, 1)),
        ("v10.20.30", (10, 20, 30)),
    ],
)
def test_parseable_tags(name, expected):
    assert parse_tag(name) == expected


@pytest.mark.parametrize(
    "name",
    ["v1.2", "1.2.3.4", "v1.2.3-rc1", "release-1.2.3", "latest", "", "v1.2.x", None],
)
def test_unparseable_tags_are_ignored(name):
    assert parse_tag(name) is None


def test_the_required_ordering_example():
    tags = ["v1.2.4", "v0.10.0", "v2.0.0", "v0.9.0", "v1.0.0"]
    assert [format_version(v) for v in sorted_versions(tags)] == [
        "0.9.0", "0.10.0", "1.0.0", "1.2.4", "2.0.0"
    ]
    assert format_version(latest_version(tags)) == "2.0.0"


def test_the_required_test_case_set():
    tags = ["v0.9.0", "v0.10.0", "v1.9.9", "v1.10.0", "v2.0.0"]
    assert [format_version(v) for v in sorted_versions(tags)] == [
        "0.9.0", "0.10.0", "1.9.9", "1.10.0", "2.0.0"
    ]
    assert format_version(latest_version(tags)) == "2.0.0"


def test_lexicographic_ordering_would_have_picked_the_wrong_tag():
    # Sorted as strings, "v0.9.0" > "v0.10.0". Numerically it is not.
    tags = ["v0.9.0", "v0.10.0"]
    assert format_version(latest_version(tags)) == "0.10.0"
    assert max(tags) == "v0.9.0"


def test_the_api_return_order_does_not_matter():
    forward = ["v0.9.0", "v0.10.0", "v1.0.0"]
    assert latest_version(forward) == latest_version(list(reversed(forward)))
    assert latest_version(["v1.0.0", "v0.9.0", "v0.10.0"]) == (1, 0, 0)


def test_the_first_returned_tag_is_not_assumed_to_be_latest():
    # A repository whose tags API happens to return an old tag first.
    assert format_version(latest_version(["v0.1.0", "v3.4.5", "v0.2.0"])) == "3.4.5"


def test_no_tags_means_zero():
    assert latest_version([]) == (0, 0, 0)
    assert latest_version(["not-a-version", "latest"]) == (0, 0, 0)


def test_duplicates_are_collapsed():
    assert sorted_versions(["v1.0.0", "1.0.0"]) == [(1, 0, 0)]


# -- bumping ---------------------------------------------------------------

@pytest.mark.parametrize(
    "labels,expected",
    [
        ([], "1.2.4"),
        (["fix"], "1.2.4"),
        (["documentation"], "1.2.4"),
        (["feature"], "1.3.0"),
        (["feat"], "1.3.0"),
        (["breaking-change"], "2.0.0"),
        (["breaking-change", "feature"], "2.0.0"),
        (["FEATURE"], "1.3.0"),
    ],
)
def test_label_driven_bumps(labels, expected):
    assert format_version(bump((1, 2, 3), labels)) == expected


def test_next_version_from_unsorted_tags():
    assert next_version(["v0.10.0", "v0.9.0"], ["feature"]) == "0.11.0"


def test_next_version_with_no_tags_starts_at_a_patch():
    assert next_version([], []) == "0.0.1"
    assert next_version([], ["feature"]) == "0.1.0"
    assert next_version([], ["breaking-change"]) == "1.0.0"


# -- explicit versions -----------------------------------------------------

def test_an_explicit_version_is_used_verbatim():
    assert next_version(["v1.0.0"], ["feature"], "2.5.1") == "2.5.1"
    assert next_version(["v1.0.0"], [], "v2.5.1") == "2.5.1"


@pytest.mark.parametrize("value", ["1.2", "abc", "v1.2.3-rc1", "1.2.3.4"])
def test_a_malformed_explicit_version_is_rejected(value):
    with pytest.raises(AutomationError, match="semantic versioning"):
        next_version(["v1.0.0"], [], value)


def test_an_explicit_version_must_move_forward():
    with pytest.raises(AutomationError, match="not greater than"):
        next_version(["v2.0.0"], [], "1.9.9")
    with pytest.raises(AutomationError, match="not greater than"):
        next_version(["v2.0.0"], [], "2.0.0")


def test_a_blank_explicit_version_falls_back_to_label_bumping():
    assert next_version(["v1.0.0"], ["feature"], "   ") == "1.1.0"
