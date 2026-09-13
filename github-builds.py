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
import math
import os
import pwd
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TIMEOUT = 12
MAX_WORKERS = 8
MAX_BODY_NOTES = 10
CHUNK_BYTES = 65536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SUBJECT_BYTES = 512 * 1024
MAX_ERROR_BYTES = 64 * 1024
GH_TOKEN_MAX_BYTES = 1024
GH_DEADLINE = 8
API_VERSION = "2022-11-28"
USER_AGENT = "github-notify-center/1.0"

# The gh CLI is the credential source when no explicit token is given, so it is
# only ever resolved from system-owned directories. User-writable PATH entries
# (~/.local/bin, mise shims, Cargo bin) are deliberately excluded: a shim there
# must never be allowed to mint credentials for us.
SYSTEM_BINDIRS = ["/usr/local/bin", "/usr/bin", "/bin"]
SYSTEM_PATH = ":".join(SYSTEM_BINDIRS)


# Deterministic runtime: the widget launches this helper with a cleared
# environment, so the process must rebuild the few variables it needs itself.
def prepare_environment():
    if not os.environ.get("HOME"):
        try:
            os.environ["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except Exception:
            pass
    # System directories only. Anything executed by this process (currently
    # just `gh`) is resolved to a verified absolute path, never via a
    # user-writable PATH entry.
    os.environ["PATH"] = SYSTEM_PATH


def _trusted_gh():
    """Resolve `gh` to a verified system binary, or None.

    The search is pinned to system-owned directories and the final realpath is
    checked to still live under /usr or /bin, so a user-writable shim (for
    example ~/.local/bin/gh) can never be used as the credential source."""
    resolved = shutil.which("gh", path=SYSTEM_PATH)
    if not resolved:
        return None
    try:
        real = os.path.realpath(resolved)
    except OSError:
        return None
    if real.startswith("/usr/") or real.startswith("/bin/"):
        return real
    return None


def _kill_group(proc):
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _read_capped_stream(stream, max_bytes, deadline):
    """Read a child's stdout up to a byte cap, within a wall-clock deadline."""
    chunks = []
    size = 0
    end = time.monotonic() + deadline
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise subprocess.SubprocessError("gh timed out")
        ready, _, _ = select.select([stream], [], [], max(0.0, remaining))
        if not ready:
            raise subprocess.SubprocessError("gh timed out")
        chunk = stream.read(4096)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise subprocess.SubprocessError("gh output exceeded limit")
        chunks.append(chunk)
    return b"".join(chunks)


def gh_token():
    """`gh auth token`, streamed under a small byte cap and deadline.

    Runs only a system-installed gh (see _trusted_gh), reads stdout
    incrementally, and kills the child's whole process group if it hangs or
    produces more than GH_TOKEN_MAX_BYTES."""
    gh = _trusted_gh()
    if not gh:
        return None
    proc = None
    token = None
    try:
        # bufsize=0 keeps proc.stdout a raw, unbuffered stream so read() returns
        # exactly what is available instead of blocking to fill a buffer; the
        # select() deadline in _read_capped_stream is then authoritative.
        proc = subprocess.Popen(
            [gh, "auth", "token"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        raw = _read_capped_stream(proc.stdout, GH_TOKEN_MAX_BYTES, GH_DEADLINE)
        proc.wait(timeout=max(0, GH_DEADLINE))
        token = raw.decode("utf-8", "replace").strip() or None
    except (OSError, subprocess.SubprocessError):
        token = None
    finally:
        if proc is not None:
            # Kill the whole group BEFORE reaping the leader: once the leader is
            # reaped its pgid is gone and orphaned children would survive.
            _kill_group(proc)
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                proc.stdout.close()
            except OSError:
                pass
    return token


def build_headers(token):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = "Bearer {0}".format(token)
    return headers


def _read_capped(response, max_bytes):
    """Read a response incrementally, refusing to buffer beyond max_bytes."""
    chunks = []
    size = 0
    while True:
        chunk = response.read(CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise urllib.error.URLError(
                "response exceeded {0} bytes".format(max_bytes))
        chunks.append(chunk)
    return b"".join(chunks)


def _json(url, headers, timeout, max_bytes):
    request = urllib.request.Request(url, headers=headers)
    response = urllib.request.urlopen(request, timeout=timeout)
    try:
        raw = _read_capped(response, max_bytes)
        body = json.loads(raw)
    finally:
        response.close()
    return body, response


def http_json(host, path, token, timeout, max_bytes=MAX_RESPONSE_BYTES):
    """GET host+path as (json body, response). Raises HTTPError on failure.
    Path is always built from the configured API origin, never from input."""
    return _json("https://{0}{1}".format(host, path),
                 build_headers(token), timeout, max_bytes)


def http_absolute(url, token, timeout, max_bytes=MAX_SUBJECT_BYTES,
                  allowed_host=None):
    """GET an absolute API URL as (json body, response).

    Credentials are only ever attached to the configured API origin: anything
    without an https scheme or whose host differs from `allowed_host` is
    refused, so a hostile subject URL can never receive the Bearer token."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("refusing non-https URL")
    if allowed_host is not None and parts.netloc != allowed_host:
        raise ValueError(
            "refusing URL outside API origin {0}".format(allowed_host))
    return _json(url, build_headers(token), timeout, max_bytes)


def http_error_message(error):
    # Read the error body through the same strict cap as success responses
    # before parsing it; unbounded json.load(error) must never be used.
    try:
        raw = _read_capped(error, MAX_ERROR_BYTES)
        body = json.loads(raw)
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
    if host == "api.github.com":
        out = gh_token()
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

    for index, note in enumerate(body):
        if not note.get("unread"):
            continue
        subject = note.get("subject") or {}
        notifications.append({
            "reason": note.get("reason") or "subscribed",
            "title": subject.get("title") or "",
            "type": subject.get("type") or "Notification",
            "repo": (note.get("repository") or {}).get("full_name", ""),
            "html": notification_html(host, note),
            "body": notification_body(args, host, note) if index < MAX_BODY_NOTES else "",
            "updated_at": note.get("updated_at") or "",
        })
    return notifications, ""


def notification_body(args, host, note):
    """Subject text behind a notification, clipped to keep the popup light.

    Thread titles alone often do not say which repository a cross-repo note is
    about (marketplace issues mention the repo in the body). Fetch the tracked
    issue's/PR's body — or its latest comment when that is empty — so the
    widget can attribute the note to the watched repo whose full name appears
    in the text. Bounded to one short request per unread note."""
    subject = note.get("subject") or {}
    candidates = [subject.get("url") or ""]
    if subject.get("latest_comment_url"):
        candidates.append(subject.get("latest_comment_url"))
    for url in candidates:
        url = _absolute_url(url)
        if not url:
            continue
        try:
            payload, _ = http_absolute(url, args.token, min(args.timeout, 8),
                                       allowed_host=host)
        except Exception:
            continue
        text = payload.get("body")
        if isinstance(text, str) and text.strip():
            return text[:2000]
    return ""


def _absolute_url(url):
    # API subject urls arrive absolute; keep only the https form.
    url = str(url or "").strip()
    if url.startswith("https://"):
        return url
    return ""


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
        "runs": (body.get("workflow_runs") or [])[: args.per_page],
        "runUrl": "https://{0}{1}".format(host, path),
        "repoUrl": "https://{0}/{1}/actions".format(web_base(host), repo),
        "rate": {
            "limit": headers.get("X-RateLimit-Limit"),
            "remaining": headers.get("X-RateLimit-Remaining"),
        },
    }


def main():
    prepare_environment()
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

    # Whole-process deadline, independent of any single request timeout: even
    # if every HTTP call hung forever (or the concurrent pool wedged), the
    # helper cannot outlive this bound. The widget keeps its own watchdog too.
    waves = max(1, math.ceil(args.max_repos / MAX_WORKERS))
    deadline = max(30, args.timeout * (waves + 4))
    signal.signal(signal.SIGALRM, lambda signum, frame: _expire())
    signal.alarm(int(deadline))

    host = (args.host or "api.github.com").rstrip("/")
    gh_available = _trusted_gh() is not None
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


def _expire():
    raise SystemExit("poll deadline exceeded")


if __name__ == "__main__":
    main()