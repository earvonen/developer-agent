from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

from llama_stack_client import LlamaStackClient

from developer_agent.config import Settings
from developer_agent.git_repo import (
    GitSource,
    clone_repository,
    commit_branch_and_push,
    git_repo_summary,
    git_source_from_clone_url,
)
from developer_agent.mcp_github import (
    GitHubIssue,
    create_pull_request_via_mcp,
    list_open_labeled_issues_via_mcp,
)
from developer_agent.llama_tools import run_tool_assisted_fix
from developer_agent.state_store import StateStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert software engineer running inside **Developer**, an automated coding agent.

You are given a **GitHub issue** (title and body) that describes a task for the repository, and a **local Git
clone** checked out on the branch named in the user message (`DEVELOPER_GIT_BRANCH`). Your goals:

1. Understand the task from the issue. Ask yourself what code or documentation changes are needed.
2. Use **GitHub MCP tools** when helpful (browse, search, commits). The workspace is already a clone of the
   configured branch; use `workspace_list_files`, `workspace_read_file`, and `workspace_write_file` under
   that clone for local edits.
3. Implement the task with **minimal, correct changes**—prefer small, reviewable diffs.
4. When work is ready, use **GitHub MCP tools** to publish: create a branch, push, and open a **pull request**.
   The PR **base** (merge target) must be **exactly** the branch named in the user message—the same as
   `DEVELOPER_GIT_BRANCH` / the local clone. Do not target `main` or the repo default unless that branch name
   was explicitly given.

If a **Kubernetes MCP** tool group is present, ignore it unless the issue explicitly requires cluster work.

Constraints:
- Prefer small, reviewable changes; do not refactor unrelated code.
- Do not commit secrets or credentials.
- When finished, summarize what you implemented and include the PR link if you have it.
"""


def _register_mcp_endpoints(client: LlamaStackClient, settings: Settings) -> None:
    for reg in settings.parsed_mcp_registrations():
        try:
            client.toolgroups.register(
                toolgroup_id=reg.toolgroup_id,
                provider_id=reg.provider_id,
                mcp_endpoint={"uri": reg.mcp_uri},
            )
            logger.info("Registered MCP toolgroup %s", reg.toolgroup_id)
        except Exception as e:
            logger.warning(
                "Could not register MCP toolgroup %s (may already exist): %s",
                reg.toolgroup_id,
                e,
            )


def _resolve_model_id(client: LlamaStackClient, configured: str | None) -> str:
    if configured:
        return configured
    models = client.models.list()
    if not models:
        raise RuntimeError("LLAMA_STACK_MODEL_ID is unset and Llama Stack returned no models")
    mid = models[0].id
    logger.info("Using first available Llama Stack model: %s", mid)
    return mid


def _issue_workspace_key(issue: GitHubIssue) -> str:
    return str(issue.number)


def _build_user_prompt(
    issue: GitHubIssue,
    git_summary: str,
    repo_path: Path,
    branch_hint: str,
    base_branch: str,
    issue_label: str,
) -> str:
    body = (issue.body or "").strip() or "(no description provided)"
    return f"""## GitHub issue

- **Number:** #{issue.number}
- **URL:** {issue.html_url}
- **Filter label:** `{issue_label}`

### Title
{issue.title}

### Body
{body}

## Local repository
Path on disk: `{repo_path}`
Configured branch (checkout + **PR base**, same branch): **`{base_branch}`**

Recent commits:
```
{git_summary}
```

Use **GitHub MCP** when you need context beyond the clone. Use workspace tools for edits under the clone.

When your changes are ready, open a pull request with suggested head branch name `{branch_hint}`.
The PR **base** **must** be **`{base_branch}`**—not `main` unless `{base_branch}` is literally `main`.

In the PR description, **reference issue #{issue.number}** (e.g. `Fixes #{issue.number}` or `Closes #{issue.number}`
if appropriate, otherwise explain how the change addresses the issue and link #{issue.number}).

Then write a short summary of the implementation (include the PR link if you have it).
"""


def process_github_issue(
    settings: Settings,
    state: StateStore,
    client: LlamaStackClient,
    model_id: str,
    src: GitSource,
    issue: GitHubIssue,
    issue_key: str,
) -> None:
    ws = Path(settings.workspace_root) / issue_key
    if ws.exists():
        shutil.rmtree(ws)

    try:
        clone_repository(src, ws, settings.github_token, settings.git_clone_depth)
    except Exception as e:
        logger.exception(
            "Clone failed for %s/%s: %s. For private repos set GITHUB_TOKEN; otherwise ensure the repo is public.",
            src.owner,
            src.repo,
            e,
        )
        state.mark_issue_processed(
            issue_key,
            {
                "reason": "clone_failed",
                "issue": issue.number,
                "title": issue.title,
            },
        )
        return

    summary = git_repo_summary(ws)
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in issue.title)[:80].strip("-")
    branch_hint = f"{settings.pr_branch_prefix}/issue-{issue.number}-{safe_title}"[:250]
    user_prompt = _build_user_prompt(
        issue,
        summary,
        ws,
        branch_hint,
        settings.git_branch,
        settings.issue_label,
    )

    logger.info(
        "Invoking Llama Stack (model=%s) for issue #%s (key %s)",
        model_id,
        issue.number,
        issue_key,
    )
    try:
        llm_summary = run_tool_assisted_fix(
            client=client,
            model_id=model_id,
            tool_group_ids=settings.tool_group_id_list,
            repo_root=ws,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_iterations=settings.max_llm_iterations,
        )
    except Exception:
        logger.exception("Llama Stack run failed for issue #%s", issue.number)
        state.mark_issue_processed(
            issue_key,
            {
                "reason": "llm_failed",
                "issue": issue.number,
                "title": issue.title,
            },
        )
        return

    logger.info("Model finished with summary (excerpt): %s", llm_summary[:2000])

    pr_url: str | None = None
    pr_via = "github_mcp"
    base_branch = (src.default_branch_hint or "").strip() or "main"
    pr_title = f"fix(#{issue.number}): {issue.title}"[:250]
    pr_body = (
        f"Automated change from **Developer** (GitHub issue agent).\n\n"
        f"Addresses #{issue.number}: [{issue.title}]({issue.html_url})\n\n"
        f"### Implementation summary\n\n{llm_summary}\n"
    )

    if settings.dry_run_no_pr:
        logger.info("DEVELOPER_DRY_RUN_NO_PR set; skipping commit, push, and MCP pull request")
        pr_via = "dry_run"
    elif settings.github_token:
        try:
            push_status = commit_branch_and_push(
                ws,
                branch_name=branch_hint,
                token=settings.github_token,
                owner=src.owner,
                repo=src.repo,
                base_branch=src.default_branch_hint,
            )
        except Exception:
            logger.exception("Failed to commit or push for %s/%s", src.owner, src.repo)
            state.mark_issue_processed(
                issue_key,
                {
                    "reason": "push_failed",
                    "issue": issue.number,
                    "title": issue.title,
                    "repository": f"{src.owner}/{src.repo}",
                    "model_summary_excerpt": llm_summary[:8000],
                },
            )
            return

        if push_status == "no_changes":
            pr_url = "(no local changes; skipping PR)"
        elif settings.mcp_create_pull_request_tool.strip():
            try:
                pr_url = create_pull_request_via_mcp(
                    client,
                    settings,
                    owner=src.owner,
                    repo=src.repo,
                    title=pr_title,
                    body=pr_body,
                    head=branch_hint,
                    base=base_branch,
                )
            except Exception:
                logger.exception("MCP create pull request failed for %s/%s", src.owner, src.repo)
                state.mark_issue_processed(
                    issue_key,
                    {
                        "reason": "pr_failed",
                        "issue": issue.number,
                        "title": issue.title,
                        "repository": f"{src.owner}/{src.repo}",
                        "model_summary_excerpt": llm_summary[:8000],
                    },
                )
                return
        else:
            logger.info(
                "DEVELOPER_MCP_CREATE_PULL_REQUEST_TOOL empty; branch pushed without opening a PR from the agent"
            )
            pr_url = None
    else:
        logger.info(
            "GITHUB_TOKEN unset: skipping local commit/push; use GitHub MCP during the model run for remote updates"
        )

    logger.info("Pull request result: %s", pr_url or "(none; see MCP / model summary)")
    state.mark_issue_processed(
        issue_key,
        {
            "issue": issue.number,
            "title": issue.title,
            "repository": f"{src.owner}/{src.repo}",
            "pull_request": pr_url,
            "pr_via": pr_via,
        },
    )


def run_forever(settings: Settings, state: StateStore) -> None:
    src = git_source_from_clone_url(settings.git_clone_url, settings.git_branch)
    if not src:
        raise RuntimeError(
            "Could not derive GitHub owner/repo from DEVELOPER_GIT_CLONE_URL; check the URL format."
        )

    client = LlamaStackClient(
        base_url=settings.llama_stack_base_url,
        api_key=settings.llama_stack_api_key,
        timeout=600.0,
    )
    _register_mcp_endpoints(client, settings)
    model_id = _resolve_model_id(client, settings.llama_stack_model_id)

    while True:
        try:
            issues = list_open_labeled_issues_via_mcp(
                client,
                settings,
                src.owner,
                src.repo,
                settings.issue_label,
            )
            pending = [
                i
                for i in issues
                if not state.is_issue_processed(_issue_workspace_key(i))
            ]
            if pending:
                logger.info(
                    "Poll: %s open issue(s) with label %r on %s/%s (%s already processed); "
                    "%s pending",
                    len(issues),
                    settings.issue_label,
                    src.owner,
                    src.repo,
                    len(issues) - len(pending),
                    len(pending),
                )
            else:
                logger.info(
                    "Poll: no pending work — %s open issue(s) with label %r on %s/%s%s; "
                    "sleeping %ss",
                    len(issues),
                    settings.issue_label,
                    src.owner,
                    src.repo,
                    " (all already processed)" if issues else "",
                    settings.poll_interval_seconds,
                )

            for issue in pending:
                issue_key = _issue_workspace_key(issue)

                logger.info(
                    "Processing issue #%s: %s",
                    issue.number,
                    issue.title[:120],
                )
                process_github_issue(
                    settings,
                    state,
                    client,
                    model_id,
                    src,
                    issue,
                    issue_key,
                )
        except Exception:
            logger.exception("Poll iteration failed")

        time.sleep(settings.poll_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings()
    state = StateStore(settings.state_file_path)
    run_forever(settings, state)


if __name__ == "__main__":
    main()
