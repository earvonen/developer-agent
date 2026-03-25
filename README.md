# Developer (developer-agent)

**Developer** is a Python service that **polls for GitHub issues** on the repository configured in its **ConfigMap** (or environment), filtered by a **label** and **interval**. Listing issues and **opening pull requests** go **only through your GitHub MCP server**, using Llama Stack’s **`tool_runtime.invoke_tool`** (no direct `api.github.com` calls from this app).

The model then implements the task in a **local git clone** (branch = **`DEVELOPER_GIT_BRANCH`**). After the run, the agent can **commit and push** over **git** when **`GITHUB_TOKEN`** is set; the **PR** is created with the configured **MCP** tool, not the GitHub REST API.

## Flow

1. Every **`DEVELOPER_POLL_INTERVAL_SECONDS`**, invokes MCP tool **`DEVELOPER_MCP_LIST_ISSUES_TOOL`** (default `list_issues`) with owner/repo/label/state, parses the JSON/text response, and keeps issues that look like GitHub issues (not pull requests).
2. **Skips** issues already in **`DEVELOPER_STATE_FILE`** under `processed_issues`.
3. **Clones** via **git** using **`DEVELOPER_GIT_CLONE_URL`** at **`DEVELOPER_GIT_BRANCH`** (`GITHUB_TOKEN` optional for private HTTPS clone).
4. Runs the **Llama Stack** model with MCP + workspace tools; the model should use **GitHub MCP** for anything that needs GitHub’s API.
5. If **`GITHUB_TOKEN`** is set and **`DEVELOPER_DRY_RUN_NO_PR`** is false: **commit + push** the feature branch, then call MCP tool **`DEVELOPER_MCP_CREATE_PULL_REQUEST_TOOL`** (default `create_pull_request`) to open the PR (**base** = **`DEVELOPER_GIT_BRANCH`**). Optional **`DEVELOPER_MCP_*_EXTRA_JSON`** merges extra kwargs for your MCP server’s schema.

## Prerequisites

- **Llama Stack** at **`LLAMA_STACK_BASE_URL`**, with GitHub MCP **registered** (same tool group IDs as **`DEVELOPER_TOOL_GROUP_IDS`**). Use **`DEVELOPER_MCP_REGISTRATIONS_JSON`** if the stack does not already expose the GitHub MCP.

## MCP tool names and kwargs

Defaults assume common parameter names (`owner`, `repo`, `state`, `labels` for listing — **`labels` is sent as a list of strings**, e.g. `["my-label"]`, which matches MCP servers that expect `[]string`). For create PR: `owner`, `repo`, `title`, `body`, `head`, `base`. If your server differs, set:

- **`DEVELOPER_MCP_LIST_ISSUES_EXTRA_JSON`** — JSON object **merged** into the list-issues kwargs (overrides keys).
- **`DEVELOPER_MCP_CREATE_PULL_REQUEST_EXTRA_JSON`** — same for create PR.

Set **`DEVELOPER_MCP_CREATE_PULL_REQUEST_TOOL`** to an empty string to push without having the agent call a create-PR tool (e.g. you rely on the model alone to open the PR via MCP).

## Run locally

```bash
cd developer-agent
pip install -e .

export DEVELOPER_GIT_CLONE_URL=https://github.com/org/application.git
export DEVELOPER_GIT_BRANCH=main
export DEVELOPER_ISSUE_LABEL=developer-task
export DEVELOPER_POLL_INTERVAL_SECONDS=120
export LLAMA_STACK_BASE_URL=http://localhost:8321
export DEVELOPER_TOOL_GROUP_IDS=mcp-github
export GITHUB_TOKEN=ghp_...   # optional: HTTPS git clone/push only

developer-agent
# or: python -m developer_agent
```

## Container image

```bash
podman build -f Containerfile -t developer-agent:latest .
```

## Configuration (environment / ConfigMap)

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `DEVELOPER_GIT_CLONE_URL` | **Yes** | — | Git clone URL; used to resolve `owner/repo` and for local clone. |
| `DEVELOPER_GIT_BRANCH` | **Yes** | — | Branch to check out; **PR base** when opening via MCP. |
| `DEVELOPER_ISSUE_LABEL` | **Yes** | — | Label filter passed into the list-issues MCP tool. |
| `LLAMA_STACK_BASE_URL` | **Yes** | — | Llama Stack HTTP base URL. |
| `DEVELOPER_TOOL_GROUP_IDS` | **Yes** | — | Comma-separated tool group IDs (must include GitHub MCP). |
| `DEVELOPER_MCP_LIST_ISSUES_TOOL` | No | `list_issues` | MCP tool name for listing issues. |
| `DEVELOPER_MCP_LIST_ISSUES_EXTRA_JSON` | No | — | JSON object merged into list-issues kwargs. |
| `DEVELOPER_MCP_CREATE_PULL_REQUEST_TOOL` | No | `create_pull_request` | MCP tool to open a PR after push; empty = skip. |
| `DEVELOPER_MCP_CREATE_PULL_REQUEST_EXTRA_JSON` | No | — | JSON object merged into create-PR kwargs. |
| `DEVELOPER_POLL_INTERVAL_SECONDS` | No | `120` | Sleep between poll loops. |
| `DEVELOPER_STATE_FILE` | No | `/tmp/developer-agent-state.json` | JSON state file. |
| `DEVELOPER_WORKSPACE_ROOT` | No | `/tmp/developer-workspaces` | Clone parent directory. |
| `DEVELOPER_GIT_CLONE_DEPTH` | No | `50` | Shallow clone depth. |
| `DEVELOPER_MAX_LLM_ITERATIONS` | No | `40` | Max tool loop rounds. |
| `DEVELOPER_PR_BRANCH_PREFIX` | No | `developer` | Branch name prefix. |
| `DEVELOPER_DRY_RUN_NO_PR` | No | `false` | Skip commit, push, and MCP PR. |
| `DEVELOPER_MCP_REGISTRATIONS_JSON` | No | — | Optional MCP SSE registrations at startup. |
| `GITHUB_TOKEN` | No | — | **Git HTTPS only** (clone/push); not used for GitHub REST. |
| `LLAMA_STACK_API_KEY` | No | — | Optional Llama Stack API key. |
| `LLAMA_STACK_MODEL_ID` | No | — | Optional model id. |

## State file

`pr_via` is typically `github_mcp`. There is no `github_rest` path anymore.

## Layout

| Path | Role |
|------|------|
| `src/developer_agent/main.py` | Poll loop, orchestration |
| `src/developer_agent/mcp_github.py` | MCP invoke for issues + PR; response parsing |
| `src/developer_agent/git_repo.py` | Clone URL → `GitSource`, clone, commit/push (git only) |
| `src/developer_agent/llama_tools.py` | Llama Stack chat + MCP tool loop |
| `src/developer_agent/config.py` | Settings |
| `src/developer_agent/state_store.py` | Processed issues |
