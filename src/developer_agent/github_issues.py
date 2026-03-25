from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str | None
    html_url: str


def _github_headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def list_open_labeled_issues(
    owner: str,
    repo: str,
    label: str,
    token: str | None,
    per_page: int = 50,
) -> list[GitHubIssue]:
    """
    List open issues (not pull requests) on the repo that have the given label.
    Oldest first. Uses GitHub REST API v3.
    """
    lab = label.strip()
    if not lab:
        return []

    issues: list[GitHubIssue] = []
    page = 1
    while True:
        q = urllib.parse.urlencode(
            {
                "state": "open",
                "labels": lab,
                "sort": "created",
                "direction": "asc",
                "per_page": str(per_page),
                "page": str(page),
            }
        )
        url = f"https://api.github.com/repos/{owner}/{repo}/issues?{q}"
        req = urllib.request.Request(url, headers=_github_headers(token), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub issues API error {e.code}: {err_body}") from e

        if not isinstance(raw, list) or not raw:
            break

        for item in raw:
            if not isinstance(item, dict):
                continue
            if item.get("pull_request"):
                continue
            num = item.get("number")
            if not isinstance(num, int):
                continue
            title = item.get("title") or ""
            body = item.get("body")
            if body is not None and not isinstance(body, str):
                body = str(body)
            html_url = str(item.get("html_url") or "")
            issues.append(
                GitHubIssue(number=num, title=str(title), body=body, html_url=html_url)
            )

        if len(raw) < per_page:
            break
        page += 1

    return issues
