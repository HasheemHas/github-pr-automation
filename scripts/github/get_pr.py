"""Fetch and validate target PR data using a GitHub App installation token."""
from __future__ import annotations
import json, os, re, urllib.request
from pathlib import Path

def request(path: str, accept: str = "application/vnd.github+json") -> bytes:
    token = os.environ["TARGET_GITHUB_TOKEN"]
    req = urllib.request.Request(f"https://api.github.com{path}", headers={"Accept": accept, "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as response: return response.read()

def main() -> None:
    full_repo, number = os.environ["TARGET_FULL_REPO"], int(os.environ["TARGET_PR_NUMBER"])
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_repo) or number < 1: raise SystemExit("Invalid target repository or PR number")
    pr = json.loads(request(f"/repos/{full_repo}/pulls/{number}"))
    if pr.get("state") != "open" or not re.fullmatch(r"[0-9a-f]{40}", pr.get("head", {}).get("sha", "")): raise SystemExit("Target PR must be open with a valid head SHA")
    files = json.loads(request(f"/repos/{full_repo}/pulls/{number}/files?per_page=100"))
    commits = json.loads(request(f"/repos/{full_repo}/pulls/{number}/commits?per_page=100"))
    output = {"number": number, "title": pr.get("title", ""), "body": pr.get("body") or "", "author": pr.get("user", {}).get("login", ""), "draft": pr.get("draft", False), "head_sha": pr["head"]["sha"], "head_ref": pr["head"]["ref"], "base_sha": pr["base"]["sha"], "base_ref": pr["base"]["ref"], "mergeable_state": pr.get("mergeable_state"), "files": [f.get("filename", "") for f in files], "commits": [c.get("commit", {}).get("message", "").split("\n", 1)[0] for c in commits], "labels": [x.get("name", "") for x in pr.get("labels", [])]}
    Path(os.environ["PR_CONTEXT_FILE"]).write_text(json.dumps(output, indent=2) + "\n")

if __name__ == "__main__": main()
