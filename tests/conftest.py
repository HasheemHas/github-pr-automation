"""Shared fakes. No test in this suite touches a real GitHub or NVIDIA endpoint."""
from __future__ import annotations

import io
import json
from typing import Any

import pytest


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(payload)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class FakeOpener:
    """Records requests and replays scripted responses keyed by 'METHOD path'."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        path = url.replace("https://api.github.com", "")
        body = json.loads(request.data.decode()) if request.data else None
        self.calls.append((method, path, body))

        key = f"{method} {path}"
        route = self.routes.get(key, self.routes.get(f"{method} *"))
        if route is None:
            raise AssertionError(f"Unexpected request: {key}")
        if isinstance(route, Exception):
            raise route
        payload, headers = route if isinstance(route, tuple) else (route, {})
        return FakeResponse(json.dumps(payload).encode() if not isinstance(payload, bytes)
                            else payload, headers)


class FakeCompletions:
    """Scripted chat.completions.create. Each entry is a payload or an exception."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("No scripted AI response left.")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        class Message:
            content = item if isinstance(item, str) else json.dumps(item)

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        return Response()


class FakeAIClient:
    def __init__(self, script: list[Any]) -> None:
        self.completions = FakeCompletions(script)

    @property
    def chat(self):
        return self


@pytest.fixture
def fake_ai():
    return FakeAIClient


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    """Point the artifact directory at a temp path."""
    directory = tmp_path / "pr-automation-artifacts"
    directory.mkdir()
    monkeypatch.setenv("PR_AUTOMATION_ARTIFACT_DIR", str(directory))
    return directory
