"""
mcp_github.py — Create GitHub issues via the GitHub REST API.

Voice commands like "create an issue", "open a ticket", "log this to GitHub"
trigger create_anomaly_issue(), which calls the GitHub API directly using httpx
(already a project dependency — no Docker or Node.js required).

Setup:
  1. Add to .env:
       GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_xxxxxxxxxxxx
       GITHUB_REPO=owner/repo-name          # e.g. acme/backend

  2. The PAT needs: Issues → Read & Write
     Settings → Developer Settings → Fine-grained tokens → New token
"""

import logging
import os

import httpx

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# ── Voice trigger detection ────────────────────────────────────────────────────
GITHUB_TRIGGER_PHRASES = [
    "create an issue",
    "create issue",
    "open an issue",
    "open issue",
    "open a ticket",
    "create a ticket",
    "log this incident",
    "log the anomaly",
    "report to github",
    "file a bug",
    "raise a ticket",
    "raise an issue",
    "github issue",
    "log to github",
]


def is_github_command(text: str) -> bool:
    t = text.lower()
    return any(phrase in t for phrase in GITHUB_TRIGGER_PHRASES)


# ── GitHub REST API ────────────────────────────────────────────────────────────
async def create_anomaly_issue(title: str, body: str) -> str:
    """
    Create a GitHub issue via the REST API and return a spoken confirmation.
    No-op with a friendly message if env vars are not set.
    """
    token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    repo_full = os.getenv("GITHUB_REPO")

    if not token or not repo_full:
        return (
            "GitHub is not configured. "
            "Please set GITHUB_PERSONAL_ACCESS_TOKEN and GITHUB_REPO in your .env file."
        )

    try:
        owner, repo_name = repo_full.split("/", 1)
    except ValueError:
        return "GITHUB_REPO must be in owner slash repo format."

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo_name}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title, "body": body},
            )
            r.raise_for_status()
            data = r.json()

        number = data.get("number", "")
        url = data.get("html_url", "")
        log.info("[GitHub] Created issue #%s: %s", number, url)
        return (
            f"Done. I've opened GitHub issue number {number} "
            f"in {repo_full}. You can find it at {url}"
        )

    except httpx.HTTPStatusError as e:
        log.error("[GitHub] API error %s: %s", e.response.status_code, e.response.text)
        return f"GitHub returned an error: {e.response.status_code}. Check your token and repo name."
    except Exception as e:
        log.error("[GitHub] Failed to create issue: %s", e)
        return f"I couldn't create the GitHub issue. Error: {e}"
