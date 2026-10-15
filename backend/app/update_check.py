"""Unauthenticated check against the PUBLIC GitHub repo for newer commits.

Security/scope, read before changing:
  * Exactly ONE outbound HTTPS GET per call, to GitHub's public REST API.
    No GitHub token, no app credential, no UW/AI key -- nothing of this
    app's is ever sent to GitHub.
  * Check-and-inform ONLY. This module never runs `git`, never touches
    Docker, never writes a file, never restarts anything. See
    README.md's "Checking for updates" section for the by-design manual/
    agent-assisted update workflow this feeds into.
  * Never raises on a network/HTTP failure -- every branch below returns a
    plain dict with a `status` the caller (main.py) passes straight to the
    frontend. No retry loop lives here; one call in = one bounded call out.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

GITHUB_REPO = "Ch0sen84/tip-hackathon-edition"
GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT_S = 6

STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_UNABLE_TO_CHECK = "unable_to_check"
STATUS_UNKNOWN_VERSION = "unknown_version"


def _repo_url() -> str:
    return f"https://github.com/{GITHUB_REPO}"


def _looks_like_commit_sha(value: Optional[str]) -> bool:
    """Format check only (hex, plausible length) -- NOT proof the commit
    actually exists in this repo. That's what the compare API call below
    is for. A value that doesn't even look like a commit (empty, "unknown",
    a made-up tag, etc.) is reported as unknown_version rather than guessed
    about."""
    if not value:
        return False
    v = value.strip().lower()
    if not (7 <= len(v) <= 40):
        return False
    return all(c in "0123456789abcdef" for c in v)


def _result(status: str, installed_commit: Optional[str], *, latest_commit=None,
            latest_commit_url=None, compare_url=None, message: str, checked_at: float) -> dict:
    return {
        "status": status,
        "installed_commit": installed_commit,
        "latest_commit": latest_commit,
        "latest_commit_url": latest_commit_url,
        "compare_url": compare_url or _repo_url(),
        "repo_url": _repo_url(),
        "message": message,
        "checked_at": checked_at,
    }


def check_for_updates(installed_commit: Optional[str], branch: str = "main") -> dict:
    """Returns a dict with one of the four statuses above. Uses GitHub's
    compare API (base=installed_commit, head=branch) rather than just
    diffing SHAs, so the answer reflects a real ancestor relationship
    (ahead/behind/identical/diverged), not merely "the strings differ"."""
    checked_at = time.time()

    if not _looks_like_commit_sha(installed_commit):
        return _result(
            STATUS_UNKNOWN_VERSION, installed_commit,
            message="No resolvable installed commit was baked into this build (version info missing) "
                    "-- cannot determine update status.",
            checked_at=checked_at,
        )

    compare_endpoint = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/compare/{installed_commit}...{branch}"
    try:
        resp = requests.get(
            compare_endpoint,
            headers={"Accept": "application/vnd.github+json"},
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as e:
        return _result(
            STATUS_UNABLE_TO_CHECK, installed_commit,
            message=f"Could not reach GitHub: {e}",
            checked_at=checked_at,
        )

    if resp.status_code in (403, 429):
        return _result(
            STATUS_UNABLE_TO_CHECK, installed_commit,
            message="GitHub API rate limit reached for unauthenticated requests -- try again later.",
            checked_at=checked_at,
        )
    if resp.status_code == 404:
        return _result(
            STATUS_UNABLE_TO_CHECK, installed_commit,
            message=f"GitHub does not recognize installed commit {installed_commit!r} in this repo "
                    "-- cannot verify its relationship to the latest commit.",
            checked_at=checked_at,
        )
    if resp.status_code != 200:
        return _result(
            STATUS_UNABLE_TO_CHECK, installed_commit,
            message=f"GitHub API returned HTTP {resp.status_code}.",
            checked_at=checked_at,
        )

    try:
        data = resp.json()
        gh_status = data["status"]  # "identical" | "ahead" | "behind" | "diverged"
        latest_sha = data["commits"][-1]["sha"] if data.get("commits") else installed_commit
        ahead_by = data.get("ahead_by")
    except (ValueError, KeyError, TypeError, IndexError):
        return _result(
            STATUS_UNABLE_TO_CHECK, installed_commit,
            message="GitHub API returned an unexpected response shape.",
            checked_at=checked_at,
        )

    latest_commit_url = f"{_repo_url()}/commit/{latest_sha}"
    compare_url = f"{_repo_url()}/compare/{installed_commit}...{branch}"

    if gh_status == "identical":
        return _result(
            STATUS_UP_TO_DATE, installed_commit, latest_commit=installed_commit,
            latest_commit_url=f"{_repo_url()}/commit/{installed_commit}", compare_url=compare_url,
            message=f"Installed build matches the latest commit on {branch}.", checked_at=checked_at,
        )
    if gh_status == "ahead":
        # `branch` (head) has commits the installed commit (base) doesn't.
        n = ahead_by if isinstance(ahead_by, int) else None
        return _result(
            STATUS_UPDATE_AVAILABLE, installed_commit, latest_commit=latest_sha,
            latest_commit_url=latest_commit_url, compare_url=compare_url,
            message=(f"{n} new commit(s) available on {branch}." if n is not None
                     else f"A newer commit is available on {branch}."),
            checked_at=checked_at,
        )
    if gh_status == "behind":
        # Installed commit is AHEAD of branch (e.g. a local/unmerged build) --
        # there is nothing newer to pull, so this is honestly "up to date",
        # not "update available".
        return _result(
            STATUS_UP_TO_DATE, installed_commit, latest_commit=installed_commit,
            latest_commit_url=f"{_repo_url()}/commit/{installed_commit}", compare_url=compare_url,
            message=f"Installed commit is already ahead of {branch} (nothing to update).",
            checked_at=checked_at,
        )
    # "diverged": the two histories share an ancestor but neither is a
    # descendant of the other -- a real ancestor relationship cannot be
    # established, so per the "never guess" rule this is reported as
    # unable_to_check (with an explicit, honest reason), never as
    # update_available.
    return _result(
        STATUS_UNABLE_TO_CHECK, installed_commit, compare_url=compare_url,
        message="Installed commit and the latest commit have diverged histories -- "
                "cannot determine a clean update relationship automatically.",
        checked_at=checked_at,
    )
