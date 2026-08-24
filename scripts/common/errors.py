"""Error types shared by the central PR automation scripts."""
from __future__ import annotations


class AutomationError(Exception):
    """Base class for every deliberate, user-facing failure."""


class ConfigError(AutomationError):
    """Configuration, allowlist or organization-variable problem."""


class DispatchError(AutomationError):
    """An unsupported or unroutable action was requested."""


class AIError(AutomationError):
    """The AI provider failed, or returned output we refuse to trust."""


class SchemaError(AIError):
    """AI output did not match the required schema."""


class GitHubError(AutomationError):
    """The GitHub REST API rejected a request."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class LimitExceeded(AutomationError):
    """The pull request is larger than the configured analysis limit."""
