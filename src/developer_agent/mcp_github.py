from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from llama_stack_client import LlamaStackClient

from developer_agent.config import Settings
from developer_agent.llama_tools import tool_invocation_content_as_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str | None
    html_url: str


def invoke_mcp_tool(client: LlamaStackClient, tool_name: str, kwargs: dict[str, Any]) -> str:
    inv = client.tool_runtime.invoke_tool(tool_name=tool_name, kwargs=kwargs)
    if inv.error_message:
        raise RuntimeError(f"MCP tool {tool_name!r} failed: {inv.error_message}")
    return tool_invocation_content_as_text(inv.content)


def _looks_like_github_issue_dict(obj: dict[str, Any]) -> bool:
    n = obj.get("number")
    if not isinstance(n, int):
        return False
    return "title" in obj or "html_url" in obj or obj.get("state") in ("open", "closed")


def _deep_find_issue_dicts(obj: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        if _looks_like_github_issue_dict(obj):
            out.append(obj)
            return
        for v in obj.values():
            _deep_find_issue_dicts(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _deep_find_issue_dicts(item, out)


def _parse_json_loose(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _raw_dicts_to_issues(raw: list[dict[str, Any]]) -> list[GitHubIssue]:
    issues: list[GitHubIssue] = []
    for item in raw:
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
        issues.append(GitHubIssue(number=num, title=str(title), body=body, html_url=html_url))
    issues.sort(key=lambda i: i.number)
    return issues


def _issues_from_mcp_payload(parsed: Any) -> list[GitHubIssue]:
    # GraphQL / MCP wrappers: {"issues": [...], "totalCount": N, ...}
    if isinstance(parsed, dict) and isinstance(parsed.get("issues"), list):
        inner = [x for x in parsed["issues"] if isinstance(x, dict)]
        if not inner:
            return []
        return _raw_dicts_to_issues(inner)

    candidates: list[dict[str, Any]] = []
    _deep_find_issue_dicts(parsed, candidates)
    if isinstance(parsed, list):
        for x in parsed:
            if isinstance(x, dict) and _looks_like_github_issue_dict(x):
                candidates.append(x)
    by_num: dict[int, dict[str, Any]] = {}
    for d in candidates:
        n = d.get("number")
        if isinstance(n, int):
            by_num[n] = d
    if not by_num:
        return []
    return _raw_dicts_to_issues(list(by_num.values()))


def list_open_labeled_issues_via_mcp(
    client: LlamaStackClient,
    settings: Settings,
    owner: str,
    repo: str,
    label: str,
) -> list[GitHubIssue]:
    lab = label.strip()
    if not lab:
        return []

    kwargs: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "state": "open",
        # Many GitHub MCP servers expect labels as []string, not a single string.
        "labels": [lab],
    }
    if settings.mcp_list_issues_extra_json:
        extra = json.loads(settings.mcp_list_issues_extra_json)
        if not isinstance(extra, dict):
            raise ValueError("DEVELOPER_MCP_LIST_ISSUES_EXTRA_JSON must be a JSON object")
        kwargs.update(extra)

    raw_labels = kwargs.get("labels")
    if isinstance(raw_labels, str):
        kwargs["labels"] = [raw_labels] if raw_labels.strip() else []
    elif raw_labels is None:
        kwargs["labels"] = []
    elif isinstance(raw_labels, list):
        kwargs["labels"] = [str(x) for x in raw_labels if str(x).strip()]

    tool = settings.mcp_list_issues_tool.strip()
    if not tool:
        raise ValueError("DEVELOPER_MCP_LIST_ISSUES_TOOL must be non-empty")

    text = invoke_mcp_tool(client, tool, kwargs)
    parsed = _parse_json_loose(text)
    if parsed is None:
        excerpt = text[:500]
        if "parameter" in excerpt.lower() and "coerc" in excerpt.lower():
            logger.warning(
                "MCP tool %r rejected arguments (check types, e.g. labels as a list of strings): %s",
                tool,
                excerpt,
            )
        else:
            logger.warning(
                "Could not parse JSON from MCP tool %r (excerpt): %s",
                tool,
                excerpt,
            )
        return []

    issues = _issues_from_mcp_payload(parsed)
    if not issues and text.strip():
        # Parsed JSON with an explicit empty ``issues`` array is success, not a warning.
        if isinstance(parsed, dict) and isinstance(parsed.get("issues"), list):
            return []
        logger.warning(
            "MCP tool %r returned no recognizable issues; raw excerpt: %s",
            tool,
            text[:500],
        )
    return issues


def _extract_pr_url_from_parsed(parsed: Any) -> str | None:
    if isinstance(parsed, dict):
        if "html_url" in parsed and isinstance(parsed["html_url"], str):
            return parsed["html_url"]
        if "url" in parsed and isinstance(parsed["url"], str) and "pull" in parsed["url"]:
            return parsed["url"]
        for k in ("pull_request", "data", "result"):
            if k in parsed:
                u = _extract_pr_url_from_parsed(parsed[k])
                if u:
                    return u
    return None


def create_pull_request_via_mcp(
    client: LlamaStackClient,
    settings: Settings,
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> str:
    tool = settings.mcp_create_pull_request_tool.strip()
    if not tool:
        raise ValueError("DEVELOPER_MCP_CREATE_PULL_REQUEST_TOOL must be non-empty")

    kwargs: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }
    if settings.mcp_create_pull_request_extra_json:
        extra = json.loads(settings.mcp_create_pull_request_extra_json)
        if not isinstance(extra, dict):
            raise ValueError("DEVELOPER_MCP_CREATE_PULL_REQUEST_EXTRA_JSON must be a JSON object")
        kwargs.update(extra)

    text = invoke_mcp_tool(client, tool, kwargs)
    parsed = _parse_json_loose(text)
    if parsed is not None:
        url = _extract_pr_url_from_parsed(parsed)
        if url:
            return url
    if text.strip().startswith("http"):
        return text.strip().split()[0]
    return text.strip() or "(MCP returned no PR URL; check tool output)"
