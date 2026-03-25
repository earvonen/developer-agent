from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class McpRegistration:
    """Optional MCP registration applied at startup (Llama Stack toolgroups.register)."""

    toolgroup_id: str
    provider_id: str
    mcp_uri: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    git_clone_url: str = Field(
        ...,
        description="HTTPS or SSH clone URL for the repository to work in",
        validation_alias="DEVELOPER_GIT_CLONE_URL",
    )
    git_branch: str = Field(
        ...,
        description="Source branch to check out; pull requests target this same branch",
        validation_alias="DEVELOPER_GIT_BRANCH",
    )
    issue_label: str = Field(
        ...,
        description="Only GitHub issues with this label are picked up",
        validation_alias="DEVELOPER_ISSUE_LABEL",
    )

    poll_interval_seconds: int = Field(120, validation_alias="DEVELOPER_POLL_INTERVAL_SECONDS")
    state_file_path: str = Field(
        "/tmp/developer-agent-state.json",
        validation_alias="DEVELOPER_STATE_FILE",
    )

    llama_stack_base_url: str = Field(..., validation_alias="LLAMA_STACK_BASE_URL")
    llama_stack_api_key: str | None = Field(None, validation_alias="LLAMA_STACK_API_KEY")
    llama_stack_model_id: str | None = Field(None, validation_alias="LLAMA_STACK_MODEL_ID")

    tool_group_ids: str = Field(
        ...,
        description="Comma-separated Llama Stack tool group IDs (include GitHub MCP for repo work)",
        validation_alias="DEVELOPER_TOOL_GROUP_IDS",
    )

    mcp_list_issues_tool: str = Field(
        "list_issues",
        description="MCP tool name (GitHub server) used to list open issues; invoked via Llama Stack",
        validation_alias="DEVELOPER_MCP_LIST_ISSUES_TOOL",
    )
    mcp_list_issues_extra_json: str | None = Field(
        None,
        description="Optional JSON object merged into list-issues MCP kwargs (override param names/values)",
        validation_alias="DEVELOPER_MCP_LIST_ISSUES_EXTRA_JSON",
    )
    mcp_create_pull_request_tool: str = Field(
        "create_pull_request",
        description="MCP tool name to open a PR after local commit/push; empty string disables this step",
        validation_alias="DEVELOPER_MCP_CREATE_PULL_REQUEST_TOOL",
    )
    mcp_create_pull_request_extra_json: str | None = Field(
        None,
        description="Optional JSON object merged into create-pull-request MCP kwargs",
        validation_alias="DEVELOPER_MCP_CREATE_PULL_REQUEST_EXTRA_JSON",
    )

    mcp_registrations_json: str | None = Field(
        None,
        validation_alias="DEVELOPER_MCP_REGISTRATIONS_JSON",
        description='Optional JSON list: [{"toolgroup_id":"mcp::x","provider_id":"model-context-protocol","mcp_uri":"http://host/sse"}]',
    )

    github_token: str | None = Field(
        None,
        validation_alias="GITHUB_TOKEN",
        description="Optional: HTTPS git clone/push to GitHub only (not the GitHub REST API). "
        "Issue listing and PR creation use MCP via Llama Stack.",
    )
    git_clone_depth: int = Field(50, validation_alias="DEVELOPER_GIT_CLONE_DEPTH")
    workspace_root: str = Field("/tmp/developer-workspaces", validation_alias="DEVELOPER_WORKSPACE_ROOT")

    max_llm_iterations: int = Field(40, validation_alias="DEVELOPER_MAX_LLM_ITERATIONS")

    pr_branch_prefix: str = Field("developer", validation_alias="DEVELOPER_PR_BRANCH_PREFIX")
    dry_run_no_pr: bool = Field(False, validation_alias="DEVELOPER_DRY_RUN_NO_PR")

    @property
    def tool_group_id_list(self) -> list[str]:
        return [x.strip() for x in self.tool_group_ids.split(",") if x.strip()]

    @field_validator("poll_interval_seconds", "git_clone_depth", "max_llm_iterations")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    def parsed_mcp_registrations(self) -> list[McpRegistration]:
        if not self.mcp_registrations_json:
            return []
        raw: list[Any] = json.loads(self.mcp_registrations_json)
        out: list[McpRegistration] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("DEVELOPER_MCP_REGISTRATIONS_JSON must be a JSON list of objects")
            out.append(
                McpRegistration(
                    toolgroup_id=str(item["toolgroup_id"]),
                    provider_id=str(item.get("provider_id") or "model-context-protocol"),
                    mcp_uri=str(item["mcp_uri"]),
                )
            )
        return out
