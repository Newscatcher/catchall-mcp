#!/usr/bin/env python3
"""
Docs review agent for the NewsCatcher MCP.

After each merge to main, this script:
1. Reads CHANGELOG.md and README.md from the MCP repo.
2. Gets the git diff for the merged changes.
3. Fetches the current MCP docs page from the NewscatcherAPI/docs repo.
4. Asks Claude to decide whether the docs page needs updating.
5. If yes, creates a PR in the docs repo with the updated MDX content.

Required environment variables:
    ANTHROPIC_API_KEY   — Claude API key
    DOCS_GITHUB_TOKEN   — GitHub PAT with contents:write on NewscatcherAPI/docs
    GIT_SHA_BEFORE      — SHA of the commit before the push (github.event.before)
    GIT_SHA_AFTER       — SHA of the latest commit (github.sha)

Optional:
    CLAUDE_MODEL        — Model to use (default: claude-sonnet-4-6)
"""

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anthropic
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

DOCS_REPO = "NewscatcherAPI/docs"
DOCS_FILE_PATH = "web-search-api/integrations/mcp.mdx"
DOCS_BASE_BRANCH = "main"

MAX_DIFF_CHARS = 15_000
MAX_README_CHARS = 5_000

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── File helpers ───────────────────────────────────────────────────────────────


def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"[File not found: {path}]"


def get_git_diff() -> str:
    """Return a human-readable summary of commits + unified diff for this push."""
    before = os.environ.get("GIT_SHA_BEFORE", "").strip()
    after = os.environ.get("GIT_SHA_AFTER", "HEAD").strip()

    null_sha = "0000000000000000000000000000000000000000"
    if not before or before == null_sha:
        before = "HEAD~5"

    try:
        log = subprocess.check_output(
            ["git", "log", "--oneline", f"{before}..{after}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        diff = subprocess.check_output(
            ["git", "diff", f"{before}..{after}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        return f"[git error: {exc}]"

    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"

    return f"Commits:\n{log}\n\nDiff:\n{diff}"


# ── GitHub API helpers ─────────────────────────────────────────────────────────


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(url: str, token: str) -> requests.Response:
    r = requests.get(url, headers=_gh_headers(token), timeout=30)
    r.raise_for_status()
    return r


def gh_post(url: str, token: str, payload: dict) -> requests.Response:
    r = requests.post(url, json=payload, headers=_gh_headers(token), timeout=30)
    r.raise_for_status()
    return r


def gh_put(url: str, token: str, payload: dict) -> requests.Response:
    r = requests.put(url, json=payload, headers=_gh_headers(token), timeout=30)
    r.raise_for_status()
    return r


def fetch_docs_file(token: str) -> tuple[str, str]:
    """Fetch the MDX file from the docs repo. Returns (content, file_sha)."""
    url = f"https://api.github.com/repos/{DOCS_REPO}/contents/{DOCS_FILE_PATH}"
    data = gh_get(url, token).json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def open_pr_exists(token: str, branch_name: str) -> bool:
    url = (
        f"https://api.github.com/repos/{DOCS_REPO}/pulls"
        f"?head=NewscatcherAPI:{branch_name}&state=open"
    )
    return len(gh_get(url, token).json()) > 0


def create_docs_pr(
    token: str,
    updated_content: str,
    file_sha: str,
    pr_summary: str,
    trigger_sha: str,
) -> str:
    """Create branch → commit → PR in the docs repo. Returns the PR HTML URL."""
    branch_name = f"mcp-docs-update-{trigger_sha[:7]}"

    if open_pr_exists(token, branch_name):
        print(f"Open PR for '{branch_name}' already exists — skipping.")
        return ""

    # Latest SHA on the base branch
    ref_data = gh_get(
        f"https://api.github.com/repos/{DOCS_REPO}/git/ref/heads/{DOCS_BASE_BRANCH}",
        token,
    ).json()
    base_sha = ref_data["object"]["sha"]

    # Create branch
    gh_post(
        f"https://api.github.com/repos/{DOCS_REPO}/git/refs",
        token,
        {"ref": f"refs/heads/{branch_name}", "sha": base_sha},
    )

    # Commit the updated file
    gh_put(
        f"https://api.github.com/repos/{DOCS_REPO}/contents/{DOCS_FILE_PATH}",
        token,
        {
            "message": (
                f"docs(mcp): update MCP integration page\n\n"
                f"Triggered by {trigger_sha[:7]} in catchall-mcp."
            ),
            "content": base64.b64encode(updated_content.encode()).decode(),
            "sha": file_sha,
            "branch": branch_name,
        },
    )

    # Open the PR
    pr = gh_post(
        f"https://api.github.com/repos/{DOCS_REPO}/pulls",
        token,
        {
            "title": "docs(mcp): update MCP integration page",
            "body": (
                "## Automated Documentation Update\n\n"
                "This PR was generated by the docs-review agent after a change "
                "was merged to `main` in `newscatcher-catchall-mcp`.\n\n"
                f"### What changed\n\n{pr_summary}\n\n"
                "---\n"
                "*Generated by `.github/scripts/docs_review_agent.py`*"
            ),
            "head": branch_name,
            "base": DOCS_BASE_BRANCH,
        },
    ).json()

    return pr["html_url"]


# ── Claude helpers ─────────────────────────────────────────────────────────────


def extract_json(text: str) -> dict:
    """Extract the JSON object from Claude's response, stripping markdown fences."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError(f"No JSON object found in Claude response:\n{text[:500]}")
    return json.loads(text[start:end])


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    docs_token = os.environ.get("DOCS_GITHUB_TOKEN")
    trigger_sha = os.environ.get("GIT_SHA_AFTER", "HEAD")

    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")
    if not docs_token:
        sys.exit("ERROR: DOCS_GITHUB_TOKEN is not set.")

    print("Reading local files…")
    changelog = read_file("CHANGELOG.md")
    readme = read_file("README.md")[:MAX_README_CHARS]
    system_prompt = read_file(".github/AGENT.md")

    print("Getting git diff…")
    commits_and_diff = get_git_diff()

    print("Fetching docs MDX from docs repo…")
    docs_content, docs_file_sha = fetch_docs_file(docs_token)

    user_message = (
        "## CHANGELOG.md\n\n"
        f"{changelog}\n\n"
        "## README.md (excerpt)\n\n"
        f"{readme}\n\n"
        "## Recent Changes (Commits + Diff)\n\n"
        f"{commits_and_diff}\n\n"
        "## Current Docs Page (mcp.mdx)\n\n"
        f"{docs_content}\n"
    )

    print(f"Calling Claude ({CLAUDE_MODEL}) to analyze…")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text
    print(f"\nClaude response:\n{raw}\n")

    result = extract_json(raw)

    if result.get("update_needed") and result.get("updated_content"):
        print("Update needed — creating PR in docs repo…")
        pr_url = create_docs_pr(
            token=docs_token,
            updated_content=result["updated_content"],
            file_sha=docs_file_sha,
            pr_summary=result.get("pr_summary") or result.get("reason", ""),
            trigger_sha=trigger_sha,
        )
        if pr_url:
            print(f"PR created: {pr_url}")
    else:
        print(f"No update needed. Reason: {result.get('reason', '—')}")


if __name__ == "__main__":
    main()
