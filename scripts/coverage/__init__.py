"""Changed-line test coverage.

"Are there tests?" is a much weaker question than "are the lines this pull
request added actually executed by a test?". This package answers the second one
by intersecting the added lines from the PR diff with a coverage report produced
by the repository's own test run.

It lives outside :mod:`scripts.insights` because it needs the checked-out tree
and the test suite's output, so it runs in the credential-free validation job
alongside the quality commands.
"""
