#!/usr/bin/env python3
"""Fetch recent GitHub Actions workflow runs for the Omarchy Build Monitor.

Called by BarWidget.qml with one --repo argument per repository. Prints one
compact JSON object per repository on its own line:

    {"repo": "owner/name", "runs": [...], "runUrl": "...", "repoUrl": "..."}
    {"repo": "owner/name", "error": "human readable message"}

and a final line describing API rate-limit usage:

    {"__meta": {"limit": 5000, "remaining": 4993}}

The widget parses line-by-line so one broken repository never corrupts the
rest of the batch.

Only the Python standard library is used, so the plugin needs no curl/jq.
The token is read from --token or the GITHUB_TOKEN environment variable.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 12
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


def fetch_repo(args, repo):
    host = (args.host or "api.github.com").rstrip("/")
    url = "https://{host}/repos/{repo}/actions/runs?per_page={per_page}".format(
        host=host, repo=repo, per_page=args.per_page
    )
    request = urllib.request.Request(url, headers=build_headers(args.token))
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = json.load(response)
            headers = response.headers
    except urllib.error.HTTPError as error:
        message = http_error_message(error)
        return {"repo": repo, "error": message}
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        return {"repo": repo, "error": "network error: {0}".format(reason or error)}

    return {
        "repo": repo,
        "runs": body.get("workflow_runs") or [],
        "runUrl": url,
        "repoUrl": "https://{host}/{repo}/actions".format(host=host, repo=repo),
        "rate": {
            "limit": headers.get("X-RateLimit-Limit"),
            "remaining": headers.get("X-RateLimit-Remaining"),
        },
    }


def http_error_message(error):
    try:
        body = json.load(error)
        message = body.get("message") or ""
    except Exception:
        message = ""
    if not message:
        message = "HTTP {0}".format(error.code)
    return message[:200]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True,
                        help="owner/name (repeatable)")
    parser.add_argument("--per-page", type=int, default=6)
    parser.add_argument("--host", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or "")
    args = parser.parse_args()

    if args.per_page < 1:
        args.per_page = 1

    rate = None
    for i, repo in enumerate(args.repo):
        result = fetch_repo(args, repo)
        if result.get("rate"):
            rate = result["rate"]
        result.pop("rate", None)
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")

    if rate:
        sys.stdout.write(json.dumps(
            {"__meta": rate}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()