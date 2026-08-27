"""Flaky test detection.

A test that fails randomly and a test that is genuinely broken produce the same
red X, and treating them the same way is how a suite ends up with a dozen tests
nobody trusts. This package separates the two by running the suite more than once
and comparing per-test outcomes, and by reading the rerun markers that test
frameworks already emit.

It runs in the credential-free validation job, because it executes the target
repository's test command.
"""
