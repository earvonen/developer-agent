# Developer (developer-agent)

**Developer** is a Python service that **polls GitHub** for **open issues** on the repository configured in its **ConfigMap** (or environment). It keeps only issues that carry a **configurable label**, at a **configurable interval**. For each new issue, it drives a **Llama Stack** model with **MCP tools** (for example **GitHub**) plus local **workspace** file tools. The model implements the task described in the issue inside a **clone** of the repo on the configured **source branch**, then opens a **pull request** with that branch as **both** checkout and **PR base** (GitHub MCP and/or in-process REST when `GITHUB_TOKEN` is set).

## Flow

1. Every **`DEVELOPER_POLL_INTERVAL_SECONDS`**, lists **open** issues on **`DEVELOPER_GIT_CLONE_URL`**’s GitHub repo that have the label **`DEVELOPER_ISSUE_LABEL`** (excluding pull requests).
2. **Skips** issues already recorded in **`DEVELOPER_STATE_FILE`** under `processed_issues`.
3. **Clones** the repo at **`DEVELOPER_GIT_BRANCH`** into a workspace (public HTTPS if no token; use **`GITHUB_TOKEN`** for private repos, the GitHub REST issues API, and optional REST PR creation).
4. Runs the model with the issue **title** and **body** as the task; the model may use GitHub MCP and workspace tools.
5. Optionally **commits, pushes, and opens a PR** via REST when **`GITHUB_TOKEN`** is set; the PR **base** is **`DEVELOPER_GIT_BRANCH`**. The PR body references the issue.

## Prerequisites

- **Llama Stack** reachable at **`LLAMA_STACK_BASE_URL`** (and optional **`LLAMA_STACK_API_KEY`** / **`LLAMA_STACK_MODEL_ID`**).
- **MCP tool groups** registered with that stack (at minimum **GitHub** for typical workflows), with IDs that match **`DEVELOPER_TOOL_GROUP_IDS`**.

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
export GITHUB_TOKEN=ghp_...   # recommended for issues API rate limits and REST PR fallback

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
| `DEVELOPER_GIT_CLONE_URL` | **Yes** | — | Git clone URL (`https://…` or `git@github.com:…`). Used to clone and to resolve `owner/repo` for GitHub API and REST PRs. |
| `DEVELOPER_GIT_BRANCH` | **Yes** | — | Branch to check out; **PR merge base is the same branch**. |
| `DEVELOPER_ISSUE_LABEL` | **Yes** | — | Only issues with this **exact** GitHub label name are processed. |
| `LLAMA_STACK_BASE_URL` | **Yes** | — | Llama Stack HTTP base URL. |
| `DEVELOPER_TOOL_GROUP_IDS` | **Yes** | — | Comma-separated tool group IDs (e.g. `mcp-github`). |
| `DEVELOPER_POLL_INTERVAL_SECONDS` | No | `120` | Sleep between poll loops. |
| `DEVELOPER_STATE_FILE` | No | `/tmp/developer-agent-state.json` | JSON file of processed issue keys (issue numbers as strings). |
| `DEVELOPER_WORKSPACE_ROOT` | No | `/tmp/developer-workspaces` | Parent directory for per-issue clone directories. |
| `DEVELOPER_GIT_CLONE_DEPTH` | No | `50` | Shallow clone depth. |
| `DEVELOPER_MAX_LLM_ITERATIONS` | No | `40` | Max chat completion rounds (tool loops). |
| `DEVELOPER_PR_BRANCH_PREFIX` | No | `developer` | Prefix for suggested feature branch names. |
| `DEVELOPER_DRY_RUN_NO_PR` | No | `false` | If `true`, skip REST PR creation after the model run. |
| `DEVELOPER_MCP_REGISTRATIONS_JSON` | No | — | JSON array to register MCP SSE endpoints at startup. |
| `GITHUB_TOKEN` | No | — | Private clone, GitHub issues listing, REST PR fallback. |
| `LLAMA_STACK_API_KEY` | No | — | Optional API key for Llama Stack. |
| `LLAMA_STACK_MODEL_ID` | No | — | Optional; otherwise the first listed model is used. |

### Optional MCP registrations

Pass as **`DEVELOPER_MCP_REGISTRATIONS_JSON`** (single-line string in a `ConfigMap`).

## State file

`DEVELOPER_STATE_FILE` stores JSON like:

```json
{
  "processed_issues": {
    "42": {
      "issue": 42,
      "title": "...",
      "repository": "org/repo",
      "pull_request": "https://github.com/org/repo/pull/99",
      "pr_via": "github_rest"
    }
  }
}
```

Delete an entry or the file to re-process an issue. Mount a **PersistentVolumeClaim** on `/var/lib/developer-agent` in OpenShift if you need state across pod restarts.

## Layout

| Path | Role |
|------|------|
| `src/developer_agent/main.py` | Poll loop, orchestration |
| `src/developer_agent/github_issues.py` | GitHub REST: list labeled open issues |
| `src/developer_agent/git_repo.py` | Clone URL → `GitSource`, clone, optional REST PR |
| `src/developer_agent/llama_tools.py` | Llama Stack tool loop (MCP + workspace tools) |
| `src/developer_agent/config.py` | Settings / env parsing |
| `src/developer_agent/state_store.py` | Processed issue persistence |
