#!/usr/bin/env python3
"""Fetch recent GitHub Actions workflow runs for the Omarchy Build Monitor.

Account mode (default): monitors every repository owned by the GitHub account
currently logged in on this machine. The account comes from the gh CLI
(`gh auth token` / `gh api user`); an explicit --token or the GITHUB_TOKEN
environment variable overrides it. Repos are newest-updated first and capped
by --max-repos.

Extra repositories can be monitored alongside the account with one --repo
argument per repository.

Prints one compact JSON object per repository on its own line:

    {"repo": "owner/name", "runs": [...], "runUrl": "...", "repoUrl": "..."}
    {"repo": "owner/name", "error": "human readable message"}

and one final line describing the account and API rate-limit usage:

    {"__meta": {"mode": "account", "account": "davidmessenger123",
                "repoCount": 5, "notLoggedIn": false, "ghAvailable": true,
                "limit": 5000, "remaining": 4993}}

When no account is logged in the script prints only the __meta line with
notLoggedIn true, so the widget can prompt the user to sign in.

The widget parses line-by-line so one broken repository never corrupts the
rest of the batch.

Only the Python standard library is used, so the plugin needs no curl/jq.
The gh CLI is used only as the credential store when no token is given.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TIMEOUT = 12
MAX_WORKERS = 8
API_VERSION = "2022-11-28"
USER_AGENT = "github-build-monitor/1.0"


def build_headers(token):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = "Bearer {0}".format(token)
    return headers


def http_json(host, path, token, timeout):
    """GET host+path as (json body, response). Raises HTTPError on failure."""
    url = "https://{0}{1}".format(host, path)
    request = urllib.request.Request(url, headers=build_headers(token))
    response = urllib.request.urlopen(request, timeout=timeout)
    return json.load(response), response


def http_error_message(error):
    try:
        body = json.load(error)
        message = body.get("message") or ""
    except Exception:
        message = ""
    if not message:
        message = "HTTP {0}".format(error.code)
    return message[:200]


def resolve_token(args, host):
    """Returns (token, source). source: token / env / gh / none."""
    if args.token:
        return args.token, "token"
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"].strip(), "env"
    # The gh token only matches github.com; a custom --host needs its own token.
    if host == "api.github.com" and shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "auth", "token"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=args.timeout, check=True,
            ).stdout.decode("utf-8", "replace").strip()
        except (subprocess.SubprocessError, OSError):
            return None, "gh"
        if out:
            return out, "gh"
    return None, "none"


def account_repos(args, host, token, timeout):
    """Account login + newest-updated owned repos, capped by --max-repos."""
    body, response = http_json(host, "/user", token, timeout)
    login = body.get("login")
    if not login:
        raise ValueError("user endpoint returned no login")
    owner_only = "/user/repos?affiliation=owner&sort=updated&per_page=100"
    repos_body, _ = http_json(host, owner_only, token, timeout)
    repos = [
        repo["full_name"]
        for repo in repos_body
        if not repo.get("archived")
    ][: args.max_repos]
    return login, repos, response


def web_base(host):
    """Human-facing web host for an API host (api.github.com -> github.com)."""
    if host == "api.github.com":
        return "github.com"
    if host.startswith("api."):
        return host[len("api."):]
    return host


def notification_html(host, note):
    """Web URL for a notification: convert the API subject URL to a
    browser-friendly one, falling back to the repository page."""
    subject = note.get("subject") or {}
    api_url = subject.get("url") or ""
    repo = (note.get("repository") or {}).get("full_name", "")
    if "/repos/" in api_url:
        path = api_url.split("/repos/", 1)[-1]
        if path:
            return "https://{0}/{1}".format(web_base(host), path)
    if repo:
        return "https://{0}/{1}".format(web_base(host), repo)
    return "https://{0}/notifications".format(web_base(host))


def fetch_notifications(args, host):
    """Unread notifications for the logged-in account, newest first."""
    notifications = []
    try:
        body, _ = http_json(host, "/notifications?per_page=100", args.token, args.timeout)
    except urllib.error.HTTPError as error:
        return [], http_error_message(error)
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        return [], "network error: {0}".format(reason or error)

    for note in body:
        if not note.get("unread"):
            continue
        subject = note.get("subject") or {}
        notifications.append({
            "reason": note.get("reason") or "subscribed",
            "title": subject.get("title") or "",
            "type": subject.get("type") or "Notification",
            "repo": (note.get("repository") or {}).get("full_name", ""),
            "html": notification_html(host, note),
            "updated_at": note.get("updated_at") or "",
        })
    return notifications, ""


def fetch_repo(args, host, repo):
    path = "/repos/{repo}/actions/runs?per_page={per_page}".format(
        repo=repo, per_page=args.per_page
    )
    try:
        body, response = http_json(host, path, args.token, args.timeout)
        headers = response.headers
    except urllib.error.HTTPError as error:
        return {"repo": repo, "error": http_error_message(error)}
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        return {"repo": repo, "error": "network error: {0}".format(reason or error)}

    return {
        "repo": repo,
        "runs": body.get("workflow_runs") or [],
        "runUrl": "https://{0}{1}".format(host, path),
        "repoUrl": "https://{0}/{1}/actions".format(web_base(host), repo),
        "rate": {
            "limit": headers.get("X-RateLimit-Limit"),
            "remaining": headers.get("X-RateLimit-Remaining"),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", default=[],
                        help="extra owner/name to monitor alongside the account (repeatable)")
    parser.add_argument("--max-repos", type=int, default=30,
                        help="how many of the account's repos to monitor, newest-updated first")
    parser.add_argument("--per-page", type=int, default=6)
    parser.add_argument("--host", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--token", default="",
                        help="explicit token (default: GITHUB_TOKEN, then gh CLI auth)")
    args = parser.parse_args()

    if args.per_page < 1:
        args.per_page = 1
    if args.max_repos < 1:
        args.max_repos = 1

    host = (args.host or "api.github.com").rstrip("/")
    gh_available = shutil.which("gh") is not None
    token, source = resolve_token(args, host)
    args.token = token

    meta = {
        "mode": "account",
        "account": "",
        "repoCount": 0,
        "authSource": source,
        "ghAvailable": gh_available,
        "limit": None,
        "remaining": None,
        "notLoggedIn": False,
        "note": "",
    }

    repos = []
    try:
        login, repos, user_response = account_repos(args, host, token, args.timeout)
        meta["account"] = login
        meta["limit"] = user_response.headers.get("X-RateLimit-Limit")
        meta["remaining"] = user_response.headers.get("X-RateLimit-Remaining")
    except urllib.error.HTTPError as error:
        meta["notLoggedIn"] = error.code in (401, 403)
        meta["note"] = http_error_message(error)
    except Exception as error:
        meta["note"] = "network error: {0}".format(error)

    if not meta["notLoggedIn"] and repos:
        # Deduplicate, keeping account repos first, then the user's extras.
        seen = set()
        ordered = []
        for repo in repos + (args.repo or []):
            if repo not in seen:
                seen.add(repo)
                ordered.append(repo)
        repos = ordered
        meta["repoCount"] = len(repos)

        if token:
            # Unread notifications first: parse-order gives the popup the
            # review/mention feedback at the top of the list.
            notifications, note_error = fetch_notifications(args, host)
            for note in notifications:
                note["__notification"] = True
                sys.stdout.write(json.dumps(note, separators=(",", ":")) + "\n")

            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(repos)))) as pool:
                results = list(pool.map(
                    lambda repo: fetch_repo(args, host, repo), repos))
        else:
            results = [{"repo": repo, "error": "no credentials"} for repo in repos]

        for result in results:
            rate = result.pop("rate", None)
            if rate:
                meta["limit"] = meta["limit"] or rate["limit"]
                meta["remaining"] = rate["remaining"]
            sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    elif not meta["notLoggedIn"] and not repos and token:
        meta["note"] = "no repositories found for {0}".format(meta["account"] or "account")

    sys.stdout.write(json.dumps({"__meta": meta}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()