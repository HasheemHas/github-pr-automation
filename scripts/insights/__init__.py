"""Deterministic pull request insights.

Every module here reads only the sanitised PR context and the unified diff. None
of them make a network call, execute target code, or need a credential, so they
all run inside the ``deterministic-analysis`` job.

The two insights that genuinely need a checked-out tree and a test run — changed
line coverage and flaky test detection — live in :mod:`scripts.coverage` and
:mod:`scripts.flaky` instead, because they belong to the credential-free
validation job.
"""
