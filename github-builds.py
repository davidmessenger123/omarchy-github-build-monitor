#!/usr/bin/env python3
"""Fetch recent GitHub Actions workflow runs for the Omarchy Build Monitor.

Account mode (default): monitors every repository owned by the GitHub account
currently logged in on this machine. The account comes from the gh CLI
(`gh auth token` / `gh api user`); the GITHUB_TOKEN environment variable
overrides it. Repos are newest-updated first and capped by --max-repos.

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
import hashlib
import json
import math
import os
import pwd
import select
import shutil
import signal
import subprocess
import sys
import threading
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
ETAG_FILE_DIR = os.path.join(".cache", "davidjm-github-builds")
ETAG_FILE_NAME = "etags.json"
ETAG_MAX_FILE_BYTES = 128 * 1024
ETAG_MAX_VALUE_LEN = 512
CACHE_ITEM_LEN = 512
TIMEOUT_EXIT = 124
STATE_LOCK = threading.Lock()

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


# Conditional-request state kept across helper runs so a poll whose data is
# unchanged costs almost no rate limit (GitHub answers 304, which is free).
# The file lives under this user's HOME (never a world-writable /tmp path),
# is only ever written atomically by this script, and stores no secrets: the
# account identity and repos for reuse on 304 plus ETag values. State is
# namespaced by a hash of host + token so switching accounts never reuses
# another account's etags.
def etag_path():
    home = os.environ.get("HOME") or ""
    if not home:
        return ""
    return os.path.join(home, ETAG_FILE_DIR, ETAG_FILE_NAME)


def etag_namespace(token, host):
    digest = hashlib.sha256()
    digest.update(host.encode("utf-8"))
    if token:
        digest.update(b"\x00")
        digest.update(token.encode("utf-8"))
    return digest.hexdigest()[:16]


def read_state(path):
    state = {"etags": {}, "cache": {}}
    if not path or not os.path.exists(path):
        return state
    try:
        with open(path, "r") as handle:
            raw = handle.read(ETAG_MAX_FILE_BYTES + 1)
        if len(raw) > ETAG_MAX_FILE_BYTES:
            return state
        data = json.loads(raw)
    except (OSError, ValueError):
        return state
    etags = data.get("etags") if isinstance(data.get("etags"), dict) else {}
    cache = data.get("cache") if isinstance(data.get("cache"), dict) else {}
    for key, value in etags.items():
        if (isinstance(key, str) and isinstance(value, str)
                and len(key) <= 256 and len(value) <= ETAG_MAX_VALUE_LEN):
            state["etags"][key] = value
    for ns, entry in cache.items():
        if not isinstance(ns, str) or len(ns) > 128 or not isinstance(entry, dict):
            continue
        clean = {}
        for key, value in entry.items():
            if not isinstance(key, str) or len(key) > 64:
                continue
            if isinstance(value, str):
                if len(value) <= CACHE_ITEM_LEN:
                    clean[key] = value
            elif isinstance(value, list):
                if (len(value) <= CACHE_ITEM_LEN
                        and all(isinstance(item, str) and len(item) <= CACHE_ITEM_LEN
                                for item in value)):
                    clean[key] = value
        state["cache"][ns] = clean
    return state


def write_state(path, state):
    if not path:
        return
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError:
        return
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


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


def _json(url, headers, timeout, max_bytes, etag=None):
    if etag:
        headers = dict(headers)
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, None
        raise
    try:
        raw = _read_capped(response, max_bytes)
        body = json.loads(raw)
    finally:
        response.close()
    return body, response


def http_json(host, path, token, timeout, max_bytes=MAX_RESPONSE_BYTES, etag=None):
    """GET host+path as (json body, response). Raises HTTPError on failure.
    Path is always built from the configured API origin, never from input.
    A 304 (the body is unchanged since the etag) returns (None, None)."""
    return _json("https://{0}{1}".format(host, path),
                 build_headers(token), timeout, max_bytes, etag)


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


def resolve_token(host):
    """Returns (token, source). source: env / gh / none.

    The token never travels through argv: the widget (or an operator) sets
    GITHUB_TOKEN and the shell injects it as the child's environment, so it
    is invisible to `ps`. Without one, the gh CLI is the credential store."""
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"].strip(), "env"
    # The gh token only matches github.com; a custom --host needs its own token.
    if host == "api.github.com":
        out = gh_token()
        if out:
            return out, "gh"
    return None, "none"


def rate_of(response):
    """(limit, remaining) strings from the response headers, if present."""
    if response is None:
        return None, None
    headers = response.headers
    return headers.get("X-RateLimit-Limit"), headers.get("X-RateLimit-Remaining")


def remember_etag(etags, key, response):
    """Store the response's ETag under key (thread-safe for the pool)."""
    if response is None or response.headers is None:
        return
    value = response.headers.get("ETag") or ""
    if not value:
        return
    with STATE_LOCK:
        etags["etags"][key] = value[:ETAG_MAX_VALUE_LEN]


def account_repos(host, token, timeout, etags, ns):
    """Account login + newest-updated owned repos, capped by --max-repos.

    Each endpoint carries the stored ETag, so an unchanged account answers
    304 and the previous login/repo list from the state cache is reused
    without burning rate limit. Returns (login, repos, limit, remaining)."""
    cache = (etags or {}).get("cache", {}).get(ns) or {}
    etag_key = lambda path: ns + "|" + path

    body, response = http_json(host, "/user", token, timeout,
                               etag=(etags or {}).get("etags", {}).get(etag_key("/user")))
    if body is None:
        login = str(cache.get("account") or "")
        repos = [name for name in cache.get("repos", []) if isinstance(name, str)]
        return login, repos, cache.get("limit"), cache.get("remaining")
    login = body.get("login")
    if not login:
        raise ValueError("user endpoint returned no login")
    remember_etag(etags, etag_key("/user"), response)

    owner_only = "/user/repos?affiliation=owner&sort=updated&per_page=100"
    repos_body, repos_response = http_json(host, owner_only, token, timeout,
                                           etag=(etags or {}).get("etags", {}).get(etag_key(owner_only)))
    if repos_body is None:
        repos = [name for name in cache.get("repos", []) if isinstance(name, str)]
    else:
        repos = [
            repo["full_name"]
            for repo in repos_body
            if not repo.get("archived")
        ]
        remember_etag(etags, etag_key(owner_only), repos_response)
    limit, remaining = rate_of(response)
    return login, repos, limit, remaining


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


def fetch_notifications(args, host, etags, ns):
    """Unread notifications for the logged-in account, newest first.

    Returns (notifications, changed, error). With a matching ETag the server
    answers 304 and `changed` is False: nothing about the inbox changed since
    the last poll, so the widget keeps its previous list instead of churning
    the popup."""
    path = "/notifications?per_page=100"
    notifications = []
    try:
        body, response = http_json(host, path, args.token, args.timeout,
                                   etag=(etags or {}).get("etags", {}).get(ns + "|" + path))
    except urllib.error.HTTPError as error:
        return [], True, http_error_message(error)
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        return [], True, "network error: {0}".format(reason or error)

    if body is None:
        return [], False, ""
    remember_etag(etags, ns + "|" + path, response)

    for index, note in enumerate(body):
        if not note.get("unread"):
            continue
        subject = note.get("subject") or {}
        notifications.append({
            "id": str(note.get("id") or ""),
            "reason": note.get("reason") or "subscribed",
            "title": subject.get("title") or "",
            "type": subject.get("type") or "Notification",
            "repo": (note.get("repository") or {}).get("full_name", ""),
            "html": notification_html(host, note),
            "body": notification_body(args, host, note) if index < MAX_BODY_NOTES else "",
            "updated_at": note.get("updated_at") or "",
        })
    return notifications, True, ""


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


def fetch_repo(args, host, repo, etags, ns):
    path = "/repos/{repo}/actions/runs?per_page={per_page}".format(
        repo=repo, per_page=args.per_page
    )
    try:
        body, response = http_json(host, path, args.token, args.timeout,
                                   etag=(etags or {}).get("etags", {}).get(ns + "|" + path))
    except urllib.error.HTTPError as error:
        return {"repo": repo, "error": http_error_message(error)}
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        return {"repo": repo, "error": "network error: {0}".format(reason or error)}

    run_url = "https://{0}{1}".format(host, path)
    repo_url = "https://{0}/{1}/actions".format(web_base(host), repo)
    if body is None:
        # 304: this repo's runs are unchanged since the last poll. Emit the
        # flag and the (cheap) navigation URLs; the widget reuses its stored
        # runs so the row is never blanked.
        return {
            "repo": repo,
            "__unchanged": True,
            "runUrl": run_url,
            "repoUrl": repo_url,
        }
    remember_etag(etags, ns + "|" + path, response)

    return {
        "repo": repo,
        "runs": (body.get("workflow_runs") or [])[: args.per_page],
        "runUrl": run_url,
        "repoUrl": repo_url,
        "rate": {
            "limit": rate_of(response)[0],
            "remaining": rate_of(response)[1],
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
    signal.signal(signal.SIGALRM, _expire)
    signal.alarm(int(deadline))

    host = (args.host or "api.github.com").rstrip("/")
    gh_available = _trusted_gh() is not None
    token, source = resolve_token(host)
    args.token = token

    state_path = etag_path()
    state = read_state(state_path)
    ns = etag_namespace(token, host)
    etags = state["etags"]

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

    limit = None
    remaining = None
    repos = []
    try:
        login, owned, limit, remaining = account_repos(host, token, args.timeout, state, ns)
        repos = owned[: args.max_repos]
        meta["account"] = login
        meta["limit"] = limit
        meta["remaining"] = remaining
        state.setdefault("cache", {})[ns] = {
            "account": login,
            "repos": (repos or [])[: CACHE_ITEM_LEN],
            "limit": str(limit) if limit is not None else "",
            "remaining": str(remaining) if remaining is not None else "",
        }
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
            notifications, changed, note_error = fetch_notifications(args, host, state, ns)
            if note_error:
                meta["note"] = note_error
            if changed:
                for note in notifications:
                    note["__notification"] = True
                    sys.stdout.write(json.dumps(note, separators=(",", ":")) + "\n")
            else:
                sys.stdout.write(json.dumps({"__notifications": "unchanged"},
                                            separators=(",", ":")) + "\n")

            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(repos)))) as pool:
                results = list(pool.map(lambda repo: fetch_repo(args, host, repo, state, ns),
                                        repos))
        else:
            results = [{"repo": repo, "error": "no credentials"} for repo in repos]

        for result in results:
            if "__unchanged" not in result:
                rate = result.pop("rate", None)
                if rate:
                    meta["limit"] = meta["limit"] or rate["limit"]
                    meta["remaining"] = meta["remaining"] or rate["remaining"]
            sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    elif not meta["notLoggedIn"] and not repos and token:
        meta["note"] = "no repositories found for {0}".format(meta["account"] or "account")

    sys.stdout.write(json.dumps({"__meta": meta}, separators=(",", ":")) + "\n")
    sys.stdout.flush()

    state["etags"] = etags
    write_state(state_path, state)


def _expire(signum, frame):
    # Surface the deadline with an async-safe write (no interpreter state from
    # inside a signal handler) and exit hard: os._exit guarantees the helper
    # cannot keep running after its budget even if a handler or GC wedge stalled
    # the normal paths.
    try:
        os.write(2, b"poll deadline exceeded\n")
    except OSError:
        pass
    os._exit(TIMEOUT_EXIT)


if __name__ == "__main__":
    main()