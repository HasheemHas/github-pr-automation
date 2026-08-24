"""Minimal GitHub REST client with Link-header pagination.

The token is only ever sent in the Authorization header. It is never logged, never
written to a file and never placed into a result artifact.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

from ..common.errors import GitHubError, LimitExceeded

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "central-pr-automation"

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        root: str = API_ROOT,
        timeout: int = 30,
        retries: int = 3,
        opener=None,
        sleep=time.sleep,
    ) -> None:
        if not token:
            raise GitHubError("A GitHub App installation token is required.")
        self._token = token
        self._root = root.rstrip("/")
        self._timeout = timeout
        self._retries = retries
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    # -- low level ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self._root}{path}"

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        accept: str = "application/vnd.github+json",
    ) -> tuple[bytes, dict[str, str]]:
        """Perform one request and return ``(payload, headers)``."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        last_error: GitHubError | None = None
        for attempt in range(1, self._retries + 1):
            request = urllib.request.Request(
                self._url(path), data=data, headers=headers, method=method.upper()
            )
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    return response.read(), dict(response.headers)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
                # Never echo the request headers back; only the API's own message.
                last_error = GitHubError(
                    f"{method.upper()} {path} failed with HTTP {exc.code}.",
                    status=exc.code,
                    body=detail[:2000],
                )
                if exc.code not in RETRY_STATUSES or attempt == self._retries:
                    raise last_error from None
            except urllib.error.URLError as exc:
                last_error = GitHubError(f"{method.upper()} {path} failed: {exc.reason}")
                if attempt == self._retries:
                    raise last_error from None
            self._sleep(min(2**attempt, 8))
        raise last_error or GitHubError(f"{method.upper()} {path} failed.")

    def request(self, method: str, path: str, *, body: Any = None) -> Any:
        payload, _ = self.request_raw(method, path, body=body)
        if not payload.strip():
            return None
        return json.loads(payload.decode("utf-8"))

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body=body)

    def get_text(self, path: str, accept: str) -> str:
        payload, _ = self.request_raw("GET", path, accept=accept)
        return payload.decode("utf-8", "replace")

    # -- pagination --------------------------------------------------------

    def iterate(self, path: str, *, per_page: int = 100) -> Iterator[Any]:
        """Yield every item across all pages, following the Link header."""
        separator = "&" if "?" in path else "?"
        url = f"{path}{separator}per_page={per_page}"
        while url:
            payload, headers = self.request_raw("GET", url)
            items = json.loads(payload.decode("utf-8")) if payload.strip() else []
            if not isinstance(items, list):
                raise GitHubError(f"GET {path} did not return a list.")
            yield from items
            match = _NEXT_LINK.search(headers.get("Link", "") or headers.get("link", "") or "")
            url = match.group(1) if match else ""

    def paginate(self, path: str, *, per_page: int = 100, limit: int | None = None,
                 label: str = "results") -> list[Any]:
        """Collect every page, failing loudly rather than truncating silently.

        Raises:
            LimitExceeded: more than ``limit`` items exist.
        """
        items: list[Any] = []
        for item in self.iterate(path, per_page=per_page):
            items.append(item)
            if limit is not None and len(items) > limit:
                raise LimitExceeded(
                    f"PR exceeds configured analysis limit: more than {limit} {label}."
                )
        return items
