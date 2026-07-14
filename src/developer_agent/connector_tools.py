from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from llama_stack_client import LlamaStackClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectorInfo:
    connector_id: str
    url: str


@dataclass(frozen=True)
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    connector_id: str


def uses_legacy_tool_runtime(client: LlamaStackClient) -> bool:
    return hasattr(client, "tool_runtime")


def _connector_id_candidates(group_id: str) -> list[str]:
    g = group_id.strip()
    if not g:
        return []
    candidates = [g]
    if g.startswith("mcp-"):
        candidates.append(g[4:])
    if g.startswith("mcp::"):
        candidates.append(g[5:])
    if "::" in g:
        candidates.append(g.split("::", 1)[1])
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _stack_http_client(client: LlamaStackClient) -> httpx.Client:
    base = str(client.base_url).rstrip("/")
    headers = dict(getattr(client, "auth_headers", None) or {})
    timeout = getattr(client, "timeout", None)
    if timeout is not None and hasattr(timeout, "read"):
        read_timeout = timeout.read
    else:
        read_timeout = 120.0
    return httpx.Client(base_url=base, headers=headers, timeout=read_timeout)


def list_connectors(client: LlamaStackClient) -> list[ConnectorInfo]:
    with _stack_http_client(client) as http:
        resp = http.get("/v1beta/connectors")
        resp.raise_for_status()
        payload = resp.json()
    rows = payload.get("data", payload if isinstance(payload, list) else [])
    out: list[ConnectorInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        connector_id = str(row.get("connector_id") or "").strip()
        url = str(row.get("url") or "").strip()
        if connector_id and url:
            out.append(ConnectorInfo(connector_id=connector_id, url=url))
    return out


def resolve_connector(group_id: str, connectors: list[ConnectorInfo]) -> ConnectorInfo | None:
    by_id = {c.connector_id: c for c in connectors}
    for candidate in _connector_id_candidates(group_id):
        hit = by_id.get(candidate)
        if hit:
            return hit
    return None


def list_connector_tools(client: LlamaStackClient, connector_id: str) -> list[McpToolDef]:
    with _stack_http_client(client) as http:
        resp = http.get(f"/v1beta/connectors/{connector_id}/tools")
        resp.raise_for_status()
        payload = resp.json()
    rows = payload.get("data", payload if isinstance(payload, list) else [])
    out: list[McpToolDef] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        schema = row.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        out.append(
            McpToolDef(
                name=name,
                description=str(row.get("description") or ""),
                input_schema=schema,
                connector_id=connector_id,
            )
        )
    return out


def _mcp_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/") + "/"
    if base.endswith("/mcp/") or base.endswith("/sse/"):
        return base.rstrip("/")
    return urljoin(base, "mcp")


def _parse_mcp_sse_or_json(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    data = json.loads(body)
    if isinstance(data, dict):
        return data
    raise RuntimeError(f"Unexpected MCP response: {body[:500]}")


def _normalize_github_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    out = dict(kwargs)
    state = out.get("state")
    if isinstance(state, str):
        upper = state.strip().upper()
        if upper in {"OPEN", "CLOSED"}:
            out["state"] = upper
    return out


def invoke_mcp_on_connector(
    connector: ConnectorInfo,
    tool_name: str,
    kwargs: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> tuple[Any, str | None]:
    endpoint = _mcp_endpoint(connector.url)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": _normalize_github_kwargs(kwargs)},
    }
    with httpx.Client(timeout=timeout) as http:
        resp = http.post(
            endpoint,
            json=payload,
            headers={"Accept": "application/json, text/event-stream"},
        )
        resp.raise_for_status()
    data = _parse_mcp_sse_or_json(resp.text)
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            return None, str(err.get("message") or err)
        return None, str(err)
    return data.get("result"), None


@dataclass(frozen=True)
class ToolInvocation:
    content: Any
    error_message: str | None = None


def invoke_tool(
    client: LlamaStackClient,
    tool_name: str,
    kwargs: dict[str, Any],
    *,
    connector_id: str | None = None,
    connectors: list[ConnectorInfo] | None = None,
) -> ToolInvocation:
    if uses_legacy_tool_runtime(client):
        inv = client.tool_runtime.invoke_tool(tool_name=tool_name, kwargs=kwargs)
        return ToolInvocation(content=inv.content, error_message=inv.error_message)

    if not connector_id:
        raise RuntimeError(f"No connector mapping for MCP tool {tool_name!r}")

    connectors = connectors or list_connectors(client)
    by_id = {c.connector_id: c for c in connectors}
    connector = by_id.get(connector_id)
    if not connector:
        raise RuntimeError(f"Connector {connector_id!r} is not registered with Llama Stack")

    content, error = invoke_mcp_on_connector(connector, tool_name, kwargs)
    return ToolInvocation(content=content, error_message=error)


def collect_mcp_tools(
    client: LlamaStackClient,
    group_ids: list[str],
) -> tuple[list[Any], dict[str, str]]:
    """
    Returns MCP tool definitions and a map tool_name -> connector_id (0.7+) or toolgroup_id (0.6).
    """
    if uses_legacy_tool_runtime(client):
        all_defs: list[Any] = []
        name_to_group: dict[str, str] = {}
        for gid in group_ids:
            defs = client.tool_runtime.list_tools(tool_group_id=gid)
            for d in defs:
                n = d.name
                if n in name_to_group:
                    logger.warning(
                        "Skipping duplicate MCP tool name %r (already from group %s, also in %s)",
                        n,
                        name_to_group[n],
                        gid,
                    )
                    continue
                name_to_group[n] = gid
                all_defs.append(d)
        return all_defs, name_to_group

    connectors = list_connectors(client)
    if not connectors:
        logger.warning("Llama Stack returned no connectors; MCP tools will be unavailable")

    all_defs: list[McpToolDef] = []
    name_to_connector: dict[str, str] = {}
    for gid in group_ids:
        connector = resolve_connector(gid, connectors)
        if not connector:
            logger.warning(
                "No connector matches tool group %r (tried %s); skipping",
                gid,
                _connector_id_candidates(gid),
            )
            continue
        for tool in list_connector_tools(client, connector.connector_id):
            if tool.name in name_to_connector:
                logger.warning(
                    "Skipping duplicate MCP tool name %r (already from connector %s, also in %s)",
                    tool.name,
                    name_to_connector[tool.name],
                    connector.connector_id,
                )
                continue
            name_to_connector[tool.name] = connector.connector_id
            all_defs.append(tool)
    return all_defs, name_to_connector
