#!/usr/bin/env python3
"""
Skills review agent for the NewsCatcher MCP.

After each merge to main, this script:
1. Reads .github/skills-config.yml to find which skills to track.
2. Fetches all tracked skill files from the integrations repo.
3. Reads CHANGELOG.md, README.md, and the git diff for the merged changes.
4. Asks Claude to decide what (if anything) needs updating across all skill files.
5. If yes, creates a single PR in the integrations repo with all changes in one commit.

Required environment variables:
    ANTHROPIC_API_KEY      — Claude API key
    SKILLS_GITHUB_TOKEN    — GitHub PAT with contents:write on the integrations repo
    GIT_SHA_BEFORE         — SHA of the commit before the push (github.event.before)
    GIT_SHA_AFTER          — SHA of the latest commit (github.sha)

Optional:
    CLAUDE_MODEL           — Model to use (default: claude-sonnet-4-6)
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import anthropic
import requests
import yaml

# ── Configuration ──────────────────────────────────────────────────────────────

CONFIG_PATH = ".github/skills-config.yml"

MAX_DIFF_CHARS = 15_000
MAX_README_CHARS = 5_000

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

SKILLS_DECISION_TOOL = {
    "name": "submit_skills_decision",
    "description": "Submit the skills review decision with any file updates needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "update_needed": {
                "type": "boolean",
                "description": "True if any skill files need updating.",
            },
            "reason": {
                "type": "string",
                "description": "Overall explanation of the decision.",
            },
            "file_updates": {
                "type": "array",
                "description": "One entry per file that needs changing. Empty array if no updates needed.",
                "items": {
                    "type": "object",
                    "properties": {
                        "skill_path": {
                            "type": "string",
                            "description": "Skill directory path, e.g. 'skills/general-use-case'.",
                        },
                        "file_name": {
                            "type": "string",
                            "description": "File name within the skill directory, e.g. 'SKILL.md' or 'references/VALIDATORS.md'.",
                        },
                        "updated_content": {
                            "type": "string",
                            "description": "Complete updated file content (not a diff or excerpt).",
                        },
                        "change_summary": {
                            "type": "string",
                            "description": "One or two sentences describing what changed in this file and why.",
                        },
                    },
                    "required": ["skill_path", "file_name", "updated_content", "change_summary"],
                },
            },
            "pr_summary": {
                "type": "string",
                "description": "PR description summarising all changes across all skill files.",
            },
        },
        "required": ["update_needed", "reason", "file_updates"],
    },
}

# ── File helpers ───────────────────────────────────────────────────────────────


def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"[File not found: {path}]"


def load_config() -> dict:
    raw = read_file(CONFIG_PATH)
    cfg = yaml.safe_load(raw)
    if not cfg or "tracked_skills" not in cfg:
        sys.exit(f"ERROR: {CONFIG_PATH} is missing or has no 'tracked_skills' entries.")
    return cfg


def get_pr_diff(pr_number: str) -> str:
    """Fetch commits + unified diff for a specific merged PR via the gh CLI.
    Works for any PR regardless of local git history depth."""
    try:
        meta = json.loads(subprocess.check_output(
            ["gh", "pr", "view", pr_number, "--json", "number,title,commits"],
            text=True, stderr=subprocess.DEVNULL,
        ))
        commit_lines = "\n".join(
            f"{c['oid'][:7]} {c['messageHeadline']}"
            for c in meta.get("commits", [])
        )
        diff = subprocess.check_output(
            ["gh", "pr", "diff", pr_number],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        return f"[Error fetching PR {pr_number}: {exc}]"

    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"

    return (
        f"PR #{meta['number']}: {meta['title']}\n\n"
        f"Commits:\n{commit_lines}\n\nDiff:\n{diff}"
    )


def get_git_diff() -> str:
    """Return commits + unified diff — from a specific PR if PR_NUMBER is set,
    otherwise from the current push (GIT_SHA_BEFORE → GIT_SHA_AFTER)."""
    pr_number = os.environ.get("PR_NUMBER", "").strip()
    if pr_number:
        return get_pr_diff(pr_number)

    before = os.environ.get("GIT_SHA_BEFORE", "").strip()
    after = os.environ.get("GIT_SHA_AFTER", "HEAD").strip()

    null_sha = "0000000000000000000000000000000000000000"
    if not before or before == null_sha:
        before = "HEAD~5"

    try:
        log = subprocess.check_output(
            ["git", "log", "--oneline", f"{before}..{after}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        diff = subprocess.check_output(
            ["git", "diff", f"{before}..{after}"],
            text=True, stderr=subprocess.DEVNULL,
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


def _raise(r: requests.Response) -> None:
    """Raise with GitHub's error body included so the log tells us exactly why."""
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} — {r.text[:500]}",
            response=r,
        )


def gh_get(url: str, token: str) -> requests.Response:
    r = requests.get(url, headers=_gh_headers(token), timeout=30)
    _raise(r)
    return r


def gh_post(url: str, token: str, payload: dict) -> requests.Response:
    r = requests.post(url, json=payload, headers=_gh_headers(token), timeout=30)
    _raise(r)
    return r


def gh_put(url: str, token: str, payload: dict) -> requests.Response:
    r = requests.put(url, json=payload, headers=_gh_headers(token), timeout=30)
    _raise(r)
    return r


def fetch_skill_files(token: str, repo: str, base_branch: str, skill: dict) -> dict[str, tuple[str, str]]:
    """
    Fetch all tracked files for a skill.
    Returns {file_name: (content, sha)} for each file.
    """
    results = {}
    for file_name in skill["files"]:
        file_path = f"{skill['path']}/{file_name}"
        url = f"https://api.github.com/repos/{repo}/contents/{file_path}?ref={base_branch}"
        try:
            data = gh_get(url, token).json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            results[file_name] = (content, data["sha"])
        except requests.HTTPError as e:
            print(f"  WARNING: Could not fetch {file_path}: {e}")
            results[file_name] = (f"[Could not fetch: {e}]", "")
    return results


def open_pr_exists(token: str, repo: str, branch_name: str) -> bool:
    url = f"https://api.github.com/repos/{repo}/pulls?head={repo.split('/')[0]}:{branch_name}&state=open"
    return len(gh_get(url, token).json()) > 0


def create_skills_pr(
    token: str,
    repo: str,
    base_branch: str,
    file_updates: list[dict],
    pr_summary: str,
    trigger_sha: str,
) -> str:
    """
    Create a branch, commit each updated file via the Contents API, then open a PR.
    Uses PUT /contents/{path} (same as the docs script) — one commit per file.
    Returns the PR HTML URL.
    """
    branch_name = f"mcp-skills-update-{trigger_sha[:7]}"

    if open_pr_exists(token, repo, branch_name):
        print(f"Open PR for '{branch_name}' already exists — skipping.")
        return ""

    base_url = f"https://api.github.com/repos/{repo}"

    # Latest commit SHA on the base branch
    ref_data = gh_get(f"{base_url}/git/ref/heads/{base_branch}", token).json()
    base_sha = ref_data["object"]["sha"]

    # Create the new branch
    gh_post(
        f"{base_url}/git/refs",
        token,
        {"ref": f"refs/heads/{branch_name}", "sha": base_sha},
    )

    # Commit each file individually via the Contents API
    for update in file_updates:
        full_path = f"{update['skill_path']}/{update['file_name']}"

        # Fetch the current file SHA on the new branch (required by the PUT endpoint)
        file_data = gh_get(
            f"{base_url}/contents/{full_path}?ref={branch_name}", token
        ).json()

        gh_put(
            f"{base_url}/contents/{full_path}",
            token,
            {
                "message": (
                    f"docs(skills): {update['change_summary']}\n\n"
                    f"Triggered by {trigger_sha[:7]} in catchall-mcp."
                ),
                "content": base64.b64encode(
                    update["updated_content"].encode("utf-8")
                ).decode(),
                "sha": file_data["sha"],
                "branch": branch_name,
            },
        )

    # Open the PR
    pr_body = (
        "## Automated Skills Update\n\n"
        "This PR was generated by the skills-review agent after a change "
        "was merged to `main` in `newscatcher-catchall-mcp`.\n\n"
        f"### What changed\n\n{pr_summary}\n\n"
        "### Files updated\n\n"
        + "\n".join(
            f"- `{u['skill_path']}/{u['file_name']}` — {u['change_summary']}"
            for u in file_updates
        )
        + "\n\n---\n*Generated by `.github/scripts/skills_review_agent.py`*"
    )

    pr = gh_post(
        f"{base_url}/pulls",
        token,
        {
            "title": "docs(skills): update skill files after MCP changes",
            "body": pr_body,
            "head": branch_name,
            "base": base_branch,
        },
    ).json()

    return pr["html_url"]


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    skills_token = os.environ.get("SKILLS_GITHUB_TOKEN")
    trigger_sha = os.environ.get("GIT_SHA_AFTER", "HEAD")

    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")
    if not skills_token:
        sys.exit("ERROR: SKILLS_GITHUB_TOKEN is not set.")

    print("Loading skills config…")
    config = load_config()
    skills_repo = config["skills_repo"]
    skills_base_branch = config["skills_base_branch"]
    tracked_skills = config["tracked_skills"]
    print(f"Tracking {len(tracked_skills)} skill(s) in {skills_repo}")

    print("Reading local MCP files…")
    changelog = read_file("CHANGELOG.md")
    readme = read_file("README.md")[:MAX_README_CHARS]
    system_prompt = read_file(".github/SKILLS_AGENT.md")

    print("Getting git diff…")
    commits_and_diff = get_git_diff()

    print("Fetching skill files from integrations repo…")
    skill_sections = []
    for skill in tracked_skills:
        print(f"  Fetching {skill['path']}…")
        files = fetch_skill_files(skills_token, skills_repo, skills_base_branch, skill)
        section = f"### Skill: {skill['path']}\n\n"
        for file_name, (content, _sha) in files.items():
            section += f"#### {file_name}\n\n{content}\n\n"
        skill_sections.append(section)

    user_message = (
        "## CHANGELOG.md\n\n"
        f"{changelog}\n\n"
        "## README.md (excerpt)\n\n"
        f"{readme}\n\n"
        "## Recent Changes (Commits + Diff)\n\n"
        f"{commits_and_diff}\n\n"
        "## Current Skill Files\n\n"
        + "\n".join(skill_sections)
    )

    print(f"Calling Claude ({CLAUDE_MODEL}) to analyze…")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=system_prompt,
        tools=[SKILLS_DECISION_TOOL],
        tool_choice={"type": "tool", "name": "submit_skills_decision"},
        messages=[{"role": "user", "content": user_message}],
    )

    result = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_skills_decision":
            result = block.input
            break

    if result is None:
        sys.exit("ERROR: Claude did not call the expected tool.")

    print(f"\nDecision: update_needed={result['update_needed']}")
    print(f"Reason: {result.get('reason', '—')}\n")

    file_updates = result.get("file_updates") or []

    if result.get("update_needed") and file_updates:
        print(f"{len(file_updates)} file(s) to update — creating PR…")
        for u in file_updates:
            print(f"  {u['skill_path']}/{u['file_name']}: {u['change_summary']}")

        pr_url = create_skills_pr(
            token=skills_token,
            repo=skills_repo,
            base_branch=skills_base_branch,
            file_updates=file_updates,
            pr_summary=result.get("pr_summary") or result.get("reason", ""),
            trigger_sha=trigger_sha,
        )
        if pr_url:
            print(f"PR created: {pr_url}")
    else:
        print(f"No update needed. Reason: {result.get('reason', '—')}")


if __name__ == "__main__":
    main()
