#!/usr/bin/env python3
"""Fetch recent GitHub Actions workflow runs for the Omarchy Build Monitor."""

import argparse
import hashlib
import ipaddress
import json
import math
import os
import pwd
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_TIMEOUT = 12
MAX_TIMEOUT = 60
MAX_DEADLINE = 6000
MAX_WORKERS = 8
MAX_BODY_NOTES = 10
MAX_NOTIFICATIONS = 100
MAX_TOTAL_REPOS = 100
MAX_EXTRA_REPOS = 100
MAX_PER_PAGE = 100
MAX_REPO_PAGES = 10
CHUNK_BYTES = 65536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SUBJECT_BYTES = 512 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_LINE_BYTES = 512 * 1024
MAX_FIELD_CHARS = 4096
MAX_URL_CHARS = 2048
MAX_ERROR_MESSAGE_CHARS = 200
MAX_REDIRECTS = 4
GH_TOKEN_MAX_BYTES = 1024
GH_DEADLINE = 8
GH_TOKEN_CACHE_TTL = 300
API_VERSION = "2022-11-28"
USER_AGENT = "github-notify-center/1.0"
ETAG_FILE_DIR = os.path.join(".cache", "davidjm-github-builds")
ETAG_FILE_NAME = "etags.json"
ETAG_MAX_FILE_BYTES = 2 * 1024 * 1024
ETAG_MAX_VALUE_BYTES = 4096
ETAG_MAX_VALUE_LEN = ETAG_MAX_VALUE_BYTES
ETAG_MAX_KEY_CHARS = 512
CACHE_MAX_REPOS = MAX_TOTAL_REPOS
CACHE_ITEM_LEN = 512
CACHE_MAX_NAMESPACES = 8
CACHE_MAX_ETAGS = 2048
CACHE_TTL = 30 * 24 * 60 * 60
CACHE_VERSION = 2
TIMEOUT_EXIT = 124
STATE_LOCK = threading.RLock()
RUN_FIELDS = (
    "name",
    "display_title",
    "status",
    "conclusion",
    "run_number",
    "head_branch",
    "updated_at",
    "created_at",
    "html_url",
)
RUN_STATUSES = {"queued", "in_progress", "completed", "waiting", "requested", "pending", "approved"}
RUN_CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale", "startup_failure"}
REPO_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$")
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
SYSTEM_BINDIRS = ["/usr/local/bin", "/usr/bin", "/bin"]
SYSTEM_PATH = ":".join(SYSTEM_BINDIRS)


class PollError(Exception):
    pass


class OutputLimitExceeded(PollError):
    pass


class StateSecurityError(PollError):
    pass


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant")


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _safe_text(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit:
        return None
    if required and not value:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


class BoundedOutput:
    def __init__(self, stream, max_bytes=MAX_OUTPUT_BYTES):
        self.stream = stream
        self.max_bytes = max_bytes
        self.used = 0
        self.lines = 0

    def emit(self, value, final=False):
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        data = raw.encode("utf-8") + b"\n"
        limit = self.max_bytes if final else max(0, self.max_bytes - 16384)
        if len(data) > MAX_OUTPUT_LINE_BYTES or self.used + len(data) > limit:
            raise OutputLimitExceeded("output limit exceeded")
        try:
            self.stream.write(data.decode("utf-8"))
        except TypeError:
            self.stream.write(data)
        self.stream.flush()
        self.used += len(data)
        self.lines += 1

    def flush(self):
        self.stream.flush()


def prepare_environment():
    if not os.environ.get("HOME"):
        try:
            os.environ["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            pass
    os.environ["PATH"] = SYSTEM_PATH


def _ensure_cache_directory(path):
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.abspath(path) != path or "\x00" in path:
        raise StateSecurityError("private directory must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    current = os.open("/", flags)
    components = [part for part in path.split(os.sep) if part]
    try:
        for index, component in enumerate(components):
            if component in (".", "..") or not component:
                raise StateSecurityError("private directory is invalid")
            try:
                next_fd = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    next_fd = os.open(component, flags, dir_fd=current)
                except OSError as error:
                    raise StateSecurityError("cannot create private directory") from error
            except OSError as error:
                raise StateSecurityError("private directory is unavailable") from error
            info = os.fstat(next_fd)
            final = index == len(components) - 1
            if (not stat.S_ISDIR(info.st_mode)
                    or (final and info.st_uid != os.geteuid())
                    or (not final and info.st_uid not in (0, os.geteuid()))
                    or (final and stat.S_IMODE(info.st_mode) & 0o077)
                    or (not final and stat.S_IMODE(info.st_mode) & 0o022
                        and not (info.st_uid == 0 and stat.S_IMODE(info.st_mode) & 0o1000))):
                os.close(next_fd)
                raise StateSecurityError("private directory has unsafe type or owner")
            os.close(current)
            current = next_fd
        return path
    except Exception:
        os.close(current)
        raise


def _cache_base():
    configured = os.environ.get("XDG_CACHE_HOME")
    if configured and os.path.isabs(configured):
        return configured
    home = os.environ.get("HOME")
    if home and os.path.isabs(home):
        return os.path.join(home, ".cache")
    return os.path.join("/tmp", "omarchy-github-build-monitor-{0}".format(os.geteuid()))


def _secure_read(path, max_bytes):
    directory = os.path.dirname(path)
    _ensure_cache_directory(directory)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    parent = None
    fd = None
    try:
        parent = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(os.path.basename(path), flags, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > max_bytes):
            raise StateSecurityError("protected state has unsafe metadata")
        chunks = []
        size = 0
        while size < before.st_size:
            chunk = os.read(fd, min(CHUNK_BYTES, before.st_size - size))
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
        after = os.fstat(fd)
        if (size != before.st_size or after.st_size != before.st_size
                or after.st_dev != before.st_dev or after.st_ino != before.st_ino
                or getattr(before, "st_mtime_ns", int(before.st_mtime * 1000000000))
                != getattr(after, "st_mtime_ns", int(after.st_mtime * 1000000000))):
            raise StateSecurityError("protected state changed while reading")
        return b"".join(chunks)
    except OSError as error:
        raise StateSecurityError("cannot open protected state") from error
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


def _secure_atomic_write(path, data):
    if not isinstance(path, str) or not os.path.isabs(path) or not isinstance(data, bytes):
        raise StateSecurityError("state path must be absolute")
    if len(data) > ETAG_MAX_FILE_BYTES:
        raise StateSecurityError("state is too large")
    directory = os.path.dirname(path)
    _ensure_cache_directory(directory)
    parent = None
    fd = None
    temporary = None
    try:
        parent = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            existing = os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
                    or existing.st_nlink != 1 or stat.S_IMODE(existing.st_mode) & 0o077
                    or existing.st_size > ETAG_MAX_FILE_BYTES):
                raise StateSecurityError("state path has unsafe metadata")
        for _ in range(10):
            temporary = ".state-{0}".format(secrets.token_hex(16))
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent,
                )
                break
            except FileExistsError:
                continue
        if fd is None:
            raise StateSecurityError("cannot create temporary state")
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("short state write")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, os.path.basename(path), src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
    except (OSError, ValueError) as error:
        raise StateSecurityError("cannot write protected state") from error
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None and parent is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        if parent is not None:
            os.close(parent)


def _token_cache_path():
    return os.path.join(_cache_base(), "omarchy-github-build-monitor", "gh-token")


def _read_token_cache():
    path = _token_cache_path()
    try:
        st = os.lstat(path)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) & 0o077):
            return None
        if time.time() - st.st_mtime > GH_TOKEN_CACHE_TTL:
            return None
        raw = _secure_read(path, GH_TOKEN_MAX_BYTES)
        token = _clean_token(raw.decode("utf-8", "strict"))
        return token
    except (OSError, UnicodeError, StateSecurityError):
        return None


def _write_token_cache(token):
    token = _clean_token(token)
    if not token:
        return
    try:
        data = token.encode("utf-8")
        if len(data) > GH_TOKEN_MAX_BYTES:
            return
        _secure_atomic_write(_token_cache_path(), data)
    except (OSError, UnicodeError, StateSecurityError):
        return


def invalidate_gh_token_cache():
    path = _token_cache_path()
    directory = os.path.dirname(path)
    try:
        _ensure_cache_directory(directory)
        parent = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            st = os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
                os.unlink(os.path.basename(path), dir_fd=parent)
                os.fsync(parent)
        finally:
            os.close(parent)
    except (OSError, StateSecurityError):
        return


def _trusted_gh():
    resolved = shutil.which("gh", path=SYSTEM_PATH)
    if not resolved:
        return None
    try:
        real = os.path.realpath(resolved)
        st = os.stat(real)
    except OSError:
        return None
    if not (real.startswith("/usr/") or real.startswith("/bin/")):
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
        return None
    return real


def _kill_group(proc):
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    if pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return


def _read_capped_stream(stream, max_bytes, deadline):
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
        chunk = stream.read(min(4096, max_bytes - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise subprocess.SubprocessError("gh output exceeded limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _gh_token_uncached():
    gh = _trusted_gh()
    if not gh:
        return None
    proc = None
    try:
        home = os.environ.get("HOME", "")
        if not os.path.isabs(home):
            home = "/nonexistent"
        environment = {
            "PATH": SYSTEM_PATH,
            "HOME": home,
            "LANG": "C",
            "LC_ALL": "C",
            "GH_PROMPT_DISABLED": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        proc = subprocess.Popen(
            [gh, "auth", "token"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
            bufsize=0,
        )
        raw = _read_capped_stream(proc.stdout, GH_TOKEN_MAX_BYTES, GH_DEADLINE)
        if proc.wait(timeout=GH_DEADLINE) != 0:
            return None
        token = raw.decode("utf-8", "strict").strip()
        return token or None
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            _kill_group(proc)
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass


def gh_token(force_refresh=False):
    if not force_refresh:
        cached = _read_token_cache()
        if cached:
            return cached
    token = _clean_token(_gh_token_uncached())
    if token:
        _write_token_cache(token)
    else:
        invalidate_gh_token_cache()
    return token


def _clean_token(token):
    if not isinstance(token, str):
        return None
    value = token.strip()
    if not value or len(value.encode("utf-8")) > GH_TOKEN_MAX_BYTES:
        return None
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        return None
    return value


def build_headers(token):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    clean = _clean_token(token)
    if clean:
        headers["Authorization"] = "Bearer {0}".format(clean)
    return headers


def _canonical_hostname(hostname):
    if not isinstance(hostname, str):
        raise ValueError("API host is invalid")
    value = hostname.strip()
    if value.startswith(".") or value.endswith(".."):
        raise ValueError("API host is invalid")
    value = value.rstrip(".")
    if not value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("API host is invalid")
    if "%" in value:
        raise ValueError("API host contains an invalid zone")
    if value.startswith("[") and value.endswith("]"):
        try:
            address = ipaddress.ip_address(value[1:-1])
        except ValueError as error:
            raise ValueError("API host is invalid") from error
        if address.version != 6:
            raise ValueError("API host bracket is invalid")
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError("API host is not trusted")
        return "[" + address.compressed + "]"
    if ":" in value:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("API host must bracket an IPv6 address") from error
        if address.version != 6:
            raise ValueError("API host must bracket an IPv6 address")
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError("API host is not trusted")
        return "[" + address.compressed + "]"
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("API host is invalid") from error
    if len(value) > 253:
        raise ValueError("API host is too long")
    labels = value.split(".")
    if any(not HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("API host is invalid")
    if value in ("localhost", "0.0.0.0", "::", "::1") or value.endswith(".localhost") or value.endswith(".local"):
        raise ValueError("API host is not trusted")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
        raise ValueError("API host is not trusted")
    return value


def canonical_api_host(value=None):
    supplied = "api.github.com" if value is None else str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in supplied):
        raise ValueError("API host is invalid")
    raw = supplied.strip()
    if not raw:
        raw = "api.github.com"
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme.lower() != "https":
            raise ValueError("API host must use HTTPS")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("API host must not contain a path")
        raw = parsed.netloc
    elif raw.startswith("//"):
        parsed = urllib.parse.urlsplit(raw)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("API host must not contain a path")
        raw = parsed.netloc
    else:
        if raw.endswith("/") and raw.count("/") == 1:
            raw = raw[:-1]
        if any(char in raw for char in "/\\?#"):
            raise ValueError("API host must be a hostname")
        parsed = urllib.parse.urlsplit("//" + raw)
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc or parsed.netloc.endswith(":"):
        raise ValueError("API host must not contain userinfo")
    try:
        hostname = _canonical_hostname(parsed.hostname or "")
        port = parsed.port
    except (TypeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith("API host"):
            raise
        raise ValueError("API host is invalid") from error
    if port is not None and (port < 1 or port > 65535):
        raise ValueError("API host port is invalid")
    if port == 443:
        port = None
    if hostname.startswith("["):
        return hostname if port is None else hostname + ":" + str(port)
    return hostname if port is None else hostname + ":" + str(port)


canonicalize_host = canonical_api_host


def _origin_from_parts(parts):
    if parts.scheme.lower() != "https":
        raise ValueError("refusing non-HTTPS URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("refusing URL userinfo")
    if parts.fragment:
        raise ValueError("refusing URL fragment")
    try:
        hostname = _canonical_hostname(parts.hostname or "")
        port = parts.port
    except (TypeError, ValueError) as error:
        raise ValueError("refusing invalid URL host") from error
    if port is not None and (port < 1 or port > 65535):
        raise ValueError("refusing invalid URL port")
    return hostname, port or 443


def url_origin(url):
    value = str(url)
    if value != value.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("refusing invalid URL")
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as error:
        raise ValueError("refusing invalid URL") from error
    return _origin_from_parts(parts)


def host_origin(host):
    try:
        return _origin_from_parts(urllib.parse.urlsplit("https://" + host))
    except ValueError as error:
        raise ValueError("invalid API origin") from error


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origin):
        super().__init__()
        self.allowed_origin = tuple(allowed_origin)
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code not in (301, 302, 303, 307, 308):
            return None
        if self.redirect_count >= MAX_REDIRECTS:
            raise urllib.error.URLError("too many redirects")
        target = urllib.parse.urljoin(req.full_url, str(newurl))
        try:
            origin = url_origin(target)
        except ValueError as error:
            raise urllib.error.URLError(str(error)) from error
        if origin != self.allowed_origin:
            raise urllib.error.URLError("refusing cross-origin redirect")
        self.redirect_count += 1
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        redirected.remove_header("Authorization")
        authorization = req.get_header("Authorization")
        if authorization:
            redirected.add_header("Authorization", authorization)
        return redirected


def _open_response(request, timeout, allowed_origin):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        SafeRedirectHandler(allowed_origin),
    )
    return opener.open(request, timeout=timeout)


def _read_capped(response, max_bytes):
    chunks = []
    size = 0
    while True:
        chunk = response.read(min(CHUNK_BYTES, max_bytes - size + 1))
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        size += len(chunk)
        if size > max_bytes:
            raise urllib.error.URLError("response exceeded {0} bytes".format(max_bytes))
        chunks.append(chunk)
    return b"".join(chunks)


def _json(url, headers, timeout, max_bytes, etag=None, allowed_origin=None):
    origin = url_origin(url)
    if allowed_origin is not None and origin != tuple(allowed_origin):
        raise ValueError("refusing URL outside API origin")
    if not isinstance(max_bytes, int) or max_bytes < 1 or max_bytes > MAX_RESPONSE_BYTES:
        raise ValueError("response limit is invalid")
    if not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout < 1 or timeout > MAX_TIMEOUT:
        raise ValueError("request timeout is invalid")
    request_headers = {str(key): str(value) for key, value in headers.items()}
    if etag:
        value = str(etag)
        if len(value.encode("utf-8")) <= ETAG_MAX_VALUE_BYTES and not any(ord(char) < 32 or ord(char) == 127 for char in value):
            request_headers["If-None-Match"] = value
    request = urllib.request.Request(url, headers=request_headers)
    try:
        response = _open_response(request, float(timeout), origin)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            error.close()
            return None, error
        raise
    try:
        try:
            final_url = response.geturl()
        except (AttributeError, ValueError):
            final_url = request.full_url
        if url_origin(final_url) != origin:
            raise ValueError("response origin changed")
        raw = _read_capped(response, max_bytes)
        body = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise
    finally:
        response.close()
    return body, response


def http_json(host, path, token, timeout, max_bytes=MAX_RESPONSE_BYTES, etag=None):
    canonical = canonical_api_host(host)
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
            or len(path) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or "\\" in path or "#" in path):
        raise ValueError("API path is invalid")
    return _json(
        "https://{0}{1}".format(canonical, path),
        build_headers(token),
        timeout,
        max_bytes,
        etag,
        host_origin(canonical),
    )


def http_absolute(url, token, timeout, max_bytes=MAX_SUBJECT_BYTES, allowed_host=None):
    if allowed_host is None:
        raise ValueError("API origin is required")
    origin = host_origin(canonical_api_host(allowed_host))
    target = url_origin(url)
    if target != origin:
        raise ValueError("refusing URL outside API origin")
    return _json(url, build_headers(token), timeout, max_bytes, None, origin)


def http_error_message(error):
    try:
        raw = _read_capped(error, MAX_ERROR_BYTES)
        body = json.loads(raw)
        message = body.get("message") if isinstance(body, dict) else ""
    except (OSError, TypeError, ValueError, urllib.error.URLError):
        message = ""
    finally:
        try:
            error.close()
        except (AttributeError, OSError, ValueError):
            pass
    if not isinstance(message, str) or not message:
        message = "HTTP {0}".format(getattr(error, "code", "error"))
    return message[:MAX_ERROR_MESSAGE_CHARS]


def resolve_token(args=None, host=None, force_refresh=False):
    if host is None:
        if args is None or isinstance(args, str):
            host = args
            args = None
        else:
            host = "api.github.com"
    canonical = canonical_api_host(host)
    explicit = _clean_token(getattr(args, "token", ""))
    if explicit:
        return explicit, "token"
    environment = _clean_token(os.environ.get("GITHUB_TOKEN"))
    if environment:
        return environment, "env"
    if canonical != "api.github.com":
        return None, "none"
    token = gh_token(force_refresh=force_refresh)
    if token:
        return token, "gh"
    return None, "none"


def valid_login(login):
    return isinstance(login, str) and bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login))


def valid_repo(repo):
    if not isinstance(repo, str) or len(repo) > 140 or repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    if (not REPO_OWNER_RE.fullmatch(owner) or not REPO_NAME_RE.fullmatch(name)
            or owner in ("constructor", "prototype", "__proto__")
            or name in ("constructor", "prototype", "__proto__")):
        return False
    return name not in (".", "..") and ".." not in name


def normalize_repos(values, limit=MAX_TOTAL_REPOS):
    if limit <= 0:
        return []
    result = []
    seen = set()
    if isinstance(values, str):
        values = values.split(",")
    for value in values or []:
        if not isinstance(value, str):
            continue
        repo = value.strip()
        if not valid_repo(repo):
            continue
        key = repo.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(repo)
        if len(result) >= limit:
            break
    return result


def repo_path(repo):
    if not valid_repo(repo):
        raise ValueError("repository name is invalid")
    return urllib.parse.quote(repo, safe="/")


def web_base(host):
    canonical = canonical_api_host(host)
    if canonical == "api.github.com":
        return "github.com"
    if canonical.startswith("api."):
        return canonical[4:]
    return canonical


def _clip(value, limit=MAX_FIELD_CHARS):
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:limit]


def notification_html(host, note):
    note = note if isinstance(note, dict) else {}
    repository = note.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    repo = repository.get("full_name", "")
    if valid_repo(repo):
        return "https://{0}/{1}".format(web_base(host), repo)
    subject = note.get("subject")
    subject = subject if isinstance(subject, dict) else {}
    api_url = subject.get("url") or ""
    try:
        parts = urllib.parse.urlsplit(str(api_url))
        pieces = parts.path.split("/")
        allowed_origins = {host_origin(canonical_api_host(host)), host_origin(web_base(host))}
        if (parts.scheme.lower() == "https" and len(pieces) >= 4 and pieces[1] == "repos"
                and url_origin(str(api_url)) in allowed_origins):
            candidate = pieces[2] + "/" + pieces[3]
            if valid_repo(candidate):
                return "https://{0}/{1}".format(web_base(host), candidate)
    except ValueError:
        pass
    return "https://{0}/notifications".format(web_base(host))


def _absolute_url(url):
    supplied = str(url or "")
    if supplied != supplied.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in supplied):
        return ""
    value = supplied
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        return ""
    if "@" in parts.netloc or parts.fragment:
        return ""
    return value


def _etag_key(namespace, path):
    return "{0}|{1}".format(namespace, path)


def _new_state():
    return {"version": CACHE_VERSION, "etags": {}, "updated": {}, "cache": {}}


def etag_path():
    return os.path.join(_cache_base(), "davidjm-github-builds", ETAG_FILE_NAME)


def etag_namespace(token, host):
    origin = host_origin(canonical_api_host(host))
    digest = hashlib.sha256()
    digest.update(b"omarchy-github-build-monitor\x00")
    digest.update(repr(origin).encode("ascii"))
    digest.update(b"\x00")
    clean = _clean_token(token)
    if clean:
        digest.update(clean.encode("utf-8"))
    return digest.hexdigest()[:32]


def _valid_cache_entry(entry):
    if not isinstance(entry, dict):
        return None
    result = {}
    account = entry.get("account", "")
    if valid_login(account):
        result["account"] = account
    elif account:
        return None
    repos = entry.get("repos", [])
    if not isinstance(repos, list):
        return None
    clean_repos = []
    seen = set()
    for repo in repos:
        if not valid_repo(repo) or repo.lower() in seen:
            continue
        seen.add(repo.lower())
        clean_repos.append(repo)
        if len(clean_repos) >= CACHE_MAX_REPOS:
            break
    result["repos"] = clean_repos
    for key in ("limit", "remaining"):
        value = entry.get(key, "")
        if isinstance(value, (str, int)) and len(str(value)) <= 64 and not any(ord(char) < 32 or ord(char) == 127 for char in str(value)):
            result[key] = str(value)
        elif value:
            return None
        else:
            result[key] = ""
    updated = entry.get("updated", 0)
    if _finite_number(updated):
        result["updated"] = max(0, int(updated))
    else:
        result["updated"] = 0
    return result


def read_state(path):
    state = _new_state()
    if not path:
        return state
    try:
        raw = _secure_read(path, ETAG_MAX_FILE_BYTES)
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, ValueError, RecursionError, StateSecurityError):
        return state
    if not isinstance(data, dict):
        return state
    version = data.get("version", CACHE_VERSION)
    if version != CACHE_VERSION:
        return state
    source_etags = data.get("etags", {})
    source_updated = data.get("updated", {})
    if not isinstance(source_updated, dict):
        source_updated = {}
    if isinstance(source_etags, dict):
        for key, raw_value in source_etags.items():
            if (not isinstance(key, str) or not key or len(key) > ETAG_MAX_KEY_CHARS
                    or any(ord(char) < 32 or ord(char) == 127 for char in key)):
                continue
            value = raw_value.get("value") if isinstance(raw_value, dict) else raw_value
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > ETAG_MAX_VALUE_BYTES:
                continue
            if "\r" in value or "\n" in value:
                continue
            state["etags"][key] = value
            updated = raw_value.get("updated") if isinstance(raw_value, dict) else source_updated.get(key, 0)
            if _finite_number(updated):
                state["updated"][key] = max(0, int(updated))
    source_cache = data.get("cache", {})
    if isinstance(source_cache, dict):
        for namespace, entry in source_cache.items():
            if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{32}", namespace):
                continue
            clean = _valid_cache_entry(entry)
            if clean is not None:
                state["cache"][namespace] = clean
    return prune_state(state)


def prune_state(state, now=None, active_namespace=None):
    if not isinstance(state, dict):
        return _new_state()
    if now is None:
        now = int(time.time())
    elif not _finite_number(now):
        now = int(time.time())
    else:
        now = int(now)
    etags = state.get("etags")
    updated = state.get("updated")
    cache = state.get("cache")
    if not isinstance(etags, dict):
        etags = {}
    if not isinstance(updated, dict):
        updated = {}
    if not isinstance(cache, dict):
        cache = {}
    etag_items = []
    for key, value in etags.items():
        if (not isinstance(key, str) or not key or len(key) > ETAG_MAX_KEY_CHARS
                or any(ord(char) < 32 or ord(char) == 127 for char in key)):
            continue
        if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > ETAG_MAX_VALUE_BYTES
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            continue
        stamp = updated.get(key, 0)
        if not _finite_number(stamp):
            stamp = 0
        if stamp and now - stamp > CACHE_TTL:
            continue
        etag_items.append((max(0, int(stamp)), key, value))
    etag_items.sort(key=lambda item: (item[1].startswith((active_namespace or "") + "|"), item[0]), reverse=True)
    clean_etags = {}
    clean_updated = {}
    for stamp, key, value in etag_items[:CACHE_MAX_ETAGS]:
        clean_etags[key] = value
        clean_updated[key] = stamp
    clean_cache = {}
    entries = []
    for namespace, entry in cache.items():
        if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{32}", namespace):
            continue
        clean = _valid_cache_entry(entry)
        if clean is None:
            continue
        if clean["updated"] and now - clean["updated"] > CACHE_TTL:
            continue
        entries.append((clean["updated"], namespace, clean))
    entries.sort(key=lambda item: (item[0], item[1]))
    if active_namespace and isinstance(cache.get(active_namespace), dict):
        clean_active = _valid_cache_entry(cache[active_namespace])
        if (clean_active is not None
                and (not clean_active["updated"] or now - clean_active["updated"] <= CACHE_TTL)
                and all(item[1] != active_namespace for item in entries)):
            entries.append((clean_active["updated"], active_namespace, clean_active))
    selected = entries[-CACHE_MAX_NAMESPACES:]
    if active_namespace and isinstance(cache.get(active_namespace), dict):
        active_clean = _valid_cache_entry(cache[active_namespace])
        if (active_clean is not None
                and (not active_clean["updated"] or now - active_clean["updated"] <= CACHE_TTL)
                and all(item[1] != active_namespace for item in selected)):
            selected = selected[1:] + [(active_clean["updated"], active_namespace, active_clean)]
    for _, namespace, clean in selected:
        clean_cache[namespace] = clean
    state["version"] = CACHE_VERSION
    state["etags"] = clean_etags
    state["updated"] = clean_updated
    state["cache"] = clean_cache
    return state


def write_state(path, state):
    if not path or not isinstance(state, dict):
        return False
    prune_state(state)
    try:
        raw = json.dumps(state, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(raw) > ETAG_MAX_FILE_BYTES:
        return False
    try:
        _secure_atomic_write(path, raw)
        return True
    except StateSecurityError:
        return False


def rate_of(response):
    if response is None:
        return None, None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None, None
    values = []
    for name in ("X-RateLimit-Limit", "X-RateLimit-Remaining"):
        value = headers.get(name)
        if value is None:
            values.append(None)
        elif isinstance(value, (int, float)) and _finite_number(value):
            values.append(value)
        elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value.strip()):
            values.append(value.strip())
        else:
            values.append(None)
    return tuple(values)


def remember_etag(state, key, response):
    if (not isinstance(key, str) or not key or len(key) > ETAG_MAX_KEY_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in key)):
        return
    if response is None or getattr(response, "headers", None) is None:
        return
    value = response.headers.get("ETag") or ""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > ETAG_MAX_VALUE_BYTES:
        return
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return
    with STATE_LOCK:
        if not isinstance(state, dict):
            return
        if "etags" in state or "version" in state or "cache" in state:
            etags = state.setdefault("etags", {})
            updated = state.setdefault("updated", {})
            if isinstance(etags, dict) and isinstance(updated, dict):
                etags[key] = value
                updated[key] = int(time.time())
        else:
            state[key] = value


def _state_etag(state, key):
    if not isinstance(state, dict):
        return None
    etags = state.get("etags")
    return etags.get(key) if isinstance(etags, dict) else None


def account_repos(args, host, token, timeout, state=None, ns=None):
    if isinstance(args, str) and isinstance(token, int) and isinstance(timeout, dict):
        legacy_args = argparse.Namespace(max_repos=30, no_etag=False, token=host, timeout=token)
        login, repos, response = account_repos(legacy_args, args, host, token, timeout, state)
        limit, remaining = rate_of(response)
        if response is None:
            cache_map = timeout.get("cache", {})
            cache = cache_map.get(state, {}) if isinstance(cache_map, dict) else {}
            limit = cache.get("limit", "")
            remaining = cache.get("remaining", "")
        return login, repos, limit, remaining
    canonical = canonical_api_host(host)
    max_repos = max(1, min(MAX_TOTAL_REPOS, int(getattr(args, "max_repos", MAX_TOTAL_REPOS))))
    if state is None:
        state = _new_state()
    namespace = ns or etag_namespace(token, canonical)
    cache_map = state.get("cache")
    if not isinstance(cache_map, dict):
        cache_map = {}
        state["cache"] = cache_map
    cache = cache_map.get(namespace, {})
    if not isinstance(cache, dict):
        cache = {}
    etag = None if getattr(args, "no_etag", False) else _state_etag(state, _etag_key(namespace, "/user"))
    user_body, user_response = http_json(canonical, "/user", token, timeout, etag=etag)
    if user_body is None:
        login = cache.get("account", "")
        if not valid_login(login):
            raise ValueError("cached user response is unavailable")
    else:
        if (not isinstance(user_body, dict)
                or not valid_login(user_body.get("login"))):
            raise ValueError("user endpoint returned malformed data")
        login = user_body["login"]
        remember_etag(state, _etag_key(namespace, "/user"), user_response)
    repos = []
    seen = set()
    page = 1
    cached_values = cache.get("repos", [])
    cached_repos = [repo for repo in cached_values if valid_repo(repo)] if isinstance(cached_values, list) else []
    while page <= MAX_REPO_PAGES and len(repos) < max_repos:
        path = "/user/repos?affiliation=owner&sort=updated&per_page=100&page={0}".format(page)
        page_etag = None if getattr(args, "no_etag", False) else _state_etag(state, _etag_key(namespace, path))
        body, response = http_json(canonical, path, token, timeout, etag=page_etag)
        if body is None:
            if page == 1:
                if not cached_repos:
                    raise ValueError("cached repository response is unavailable")
                for repo in cached_repos:
                    if repo.lower() not in seen:
                        seen.add(repo.lower())
                        repos.append(repo)
                page = max(2, (len(cached_repos) // 100) + 2)
            break
        if not isinstance(body, list) or len(body) > 100:
            raise ValueError("repository endpoint returned malformed data")
        for item in body:
            if not isinstance(item, dict):
                raise ValueError("repository endpoint returned malformed data")
            name = item.get("full_name")
            if not valid_repo(name) or ("archived" in item and not isinstance(item.get("archived"), bool)):
                raise ValueError("repository endpoint returned malformed data")
            if not item.get("archived") and name.lower() not in seen:
                seen.add(name.lower())
                repos.append(name)
                if len(repos) >= max_repos:
                    break
        remember_etag(state, _etag_key(namespace, path), response)
        if len(body) < 100:
            break
        page += 1
    if not repos and cached_repos:
        repos = cached_repos[:max_repos]
    repos = repos[:max_repos]
    user_limit, user_remaining = rate_of(user_response)
    cache_entry = _valid_cache_entry({
        "account": login,
        "repos": repos,
        "limit": str(user_limit if user_limit is not None else cache.get("limit", "")),
        "remaining": str(user_remaining if user_remaining is not None else cache.get("remaining", "")),
        "updated": int(time.time()),
    })
    if cache_entry is not None:
        state.setdefault("cache", {})[namespace] = cache_entry
    return login, repos, user_response


def notification_body(args, host, note):
    subject = note.get("subject") or {}
    candidates = [subject.get("url") or ""]
    if subject.get("latest_comment_url"):
        candidates.append(subject.get("latest_comment_url"))
    for value in candidates:
        url = _absolute_url(value)
        if not url:
            continue
        try:
            payload, _ = http_absolute(url, args.token, min(args.timeout, 8), allowed_host=host)
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
            continue
        text = payload.get("body") if isinstance(payload, dict) else None
        if isinstance(text, str) and text.strip():
            return text[:2000]
    return ""


def fetch_notifications(args, host, state=None, ns=None, return_changed=False):
    return_three = return_changed or state is not None or ns is not None
    namespace = ns or etag_namespace(getattr(args, "token", None), host)
    path = "/notifications?per_page=100"
    etag = None if getattr(args, "no_etag", False) else _state_etag(state, _etag_key(namespace, path))
    try:
        body, response = http_json(host, path, args.token, args.timeout, etag=etag)
    except urllib.error.HTTPError as error:
        result = ([], http_error_message(error), True)
        return result if return_three else result[:2]
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", None)
        result = ([], "network error: {0}".format(reason or error), True)
        return result if return_three else result[:2]
    if body is None:
        result = ([], "", False)
        return result if return_three else result[:2]
    if not isinstance(body, list):
        result = ([], "malformed notifications response", True)
        return result if return_three else result[:2]
    remember_etag(state, _etag_key(namespace, path), response)
    if len(body) > MAX_NOTIFICATIONS:
        result = ([], "malformed notifications response", True)
        return result if return_three else result[:2]
    notifications = []
    for note in body:
        if not isinstance(note, dict) or note.get("unread") is not True:
            continue
        subject = note.get("subject") or {}
        repository = note.get("repository") or {}
        if not isinstance(subject, dict) or not isinstance(repository, dict):
            result = ([], "malformed notifications response", True)
            return result if return_three else result[:2]
        note_id = note.get("id")
        if note_id is not None and not isinstance(note_id, (str, int)):
            result = ([], "malformed notifications response", True)
            return result if return_three else result[:2]
        fields = {
            "reason": note.get("reason") or "subscribed",
            "title": subject.get("title") or "",
            "type": subject.get("type") or "Notification",
            "repo": repository.get("full_name") or "",
            "updated_at": note.get("updated_at") or "",
        }
        clean = {}
        for key, value in fields.items():
            clean[key] = _safe_text(value, 128 if key != "title" else MAX_FIELD_CHARS)
            if clean[key] is None:
                result = ([], "malformed notifications response", True)
                return result if return_three else result[:2]
        if clean["repo"] and not valid_repo(clean["repo"]):
            result = ([], "malformed notifications response", True)
            return result if return_three else result[:2]
        clean_id = _safe_text(str(note_id) if note_id is not None else "", 128)
        if clean_id is None:
            result = ([], "malformed notifications response", True)
            return result if return_three else result[:2]
        notifications.append({
            "id": clean_id,
            "reason": clean["reason"],
            "title": clean["title"],
            "type": clean["type"],
            "repo": clean["repo"],
            "html": _clip(notification_html(host, note), MAX_URL_CHARS),
            "body": "",
            "updated_at": clean["updated_at"],
        })
        if len(notifications) >= MAX_NOTIFICATIONS:
            break
    if getattr(args, "fetch_bodies", False):
        enrich = notifications[:MAX_BODY_NOTES]
        if enrich:
            bodies = {}
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(enrich))) as pool:
                futures = {
                    pool.submit(notification_body, args, host, next((item for item in body if isinstance(item, dict)
                             and str(item.get("id") or "") == str(note["id"])), {})): index
                    for index, note in enumerate(enrich)
                }
                for future, index in futures.items():
                    try:
                        bodies[index] = future.result()
                    except Exception:
                        bodies[index] = ""
            for index, note in enumerate(enrich):
                note["body"] = bodies.get(index, "")
    result = (notifications, "", True)
    return result if return_three else result[:2]


def trim_run(run, web_origin=None):
    if not isinstance(run, dict):
        return {}
    if run.get("status") is not None and (not isinstance(run.get("status"), str) or run["status"] not in RUN_STATUSES):
        return {}
    if run.get("conclusion") is not None and (not isinstance(run.get("conclusion"), str) or run["conclusion"] not in RUN_CONCLUSIONS):
        return {}
    clean = {}
    for key in RUN_FIELDS:
        if key not in run or run[key] is None:
            continue
        value = run[key]
        if key == "run_number":
            if isinstance(value, bool):
                return {}
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    return {}
            if not _finite_number(value) or value < 0 or value != int(value):
                return {}
            clean[key] = int(value)
        elif key == "html_url":
            if not isinstance(value, str) or len(value) > MAX_URL_CHARS:
                return {}
            try:
                origin = url_origin(value)
                if web_origin is not None and origin != tuple(web_origin):
                    return {}
            except ValueError:
                return {}
            clean[key] = value
        else:
            clean_value = _safe_text(value, MAX_FIELD_CHARS)
            if clean_value is None:
                return {}
            clean[key] = clean_value
    if not clean:
        return {}
    return clean


def fetch_repo(args, host, repo, state=None, ns=None):
    if not valid_repo(repo):
        return {"repo": str(repo)[:256], "error": "invalid repository name"}
    per_page = max(1, min(MAX_PER_PAGE, int(getattr(args, "per_page", 6))))
    path = "/repos/{0}/actions/runs?per_page={1}".format(repo_path(repo), per_page)
    namespace = ns or etag_namespace(getattr(args, "token", None), host)
    etag = None if getattr(args, "no_etag", False) else _state_etag(state, _etag_key(namespace, path))
    try:
        body, response = http_json(host, path, args.token, args.timeout, etag=etag)
    except urllib.error.HTTPError as error:
        return {"repo": repo, "error": http_error_message(error)}
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", None)
        return {"repo": repo, "error": "network error: {0}".format(reason or error)}
    run_url = "https://{0}{1}".format(canonical_api_host(host), path)
    repo_url = "https://{0}/{1}/actions".format(web_base(host), repo)
    if body is None:
        return {"repo": repo, "__unchanged": True, "runUrl": run_url, "repoUrl": repo_url}
    if (not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list)
            or len(body["workflow_runs"]) > MAX_PER_PAGE):
        return {"repo": repo, "error": "malformed workflow response"}
    runs = []
    try:
        web_origin = host_origin(web_base(host))
    except ValueError:
        return {"repo": repo, "error": "invalid API origin"}
    for run in body["workflow_runs"][:per_page]:
        if not isinstance(run, dict):
            return {"repo": repo, "error": "malformed workflow response"}
        clean = trim_run(run, web_origin)
        if not clean:
            return {"repo": repo, "error": "malformed workflow response"}
        runs.append(clean)
    remember_etag(state, _etag_key(namespace, path), response)
    limit, remaining = rate_of(response)
    return {
        "repo": repo,
        "runs": runs,
        "runUrl": run_url,
        "repoUrl": repo_url,
        "rate": {"limit": limit, "remaining": remaining},
    }


def poll_deadline_seconds(timeout, max_repos, extras=0, per_page=6, fetch_bodies=False):
    timeout = max(1, min(MAX_TIMEOUT, int(timeout)))
    per_page = max(1, min(MAX_PER_PAGE, int(per_page)))
    total_repos = min(MAX_TOTAL_REPOS, max(1, int(max_repos)) + min(MAX_EXTRA_REPOS, max(0, int(extras))))
    repo_waves = max(1, int(math.ceil(total_repos / float(MAX_WORKERS))))
    page_work = MAX_REPO_PAGES * max(1, int(math.ceil(per_page / 30.0)))
    notification_work = 1 + (MAX_BODY_NOTES * 2 if fetch_bodies else 0)
    notification_waves = max(1, notification_work)
    work = 2 + page_work + repo_waves + notification_waves
    return min(MAX_DEADLINE, max(30, GH_DEADLINE + timeout * work + 5))


def _expire(signum, frame):
    try:
        os.write(2, b"poll deadline exceeded\n")
    except OSError:
        pass
    os._exit(TIMEOUT_EXIT)


def _meta(session, source="none", gh_available=False, note="", web_origin=""):
    return {
        "mode": "account",
        "account": "",
        "webOrigin": web_origin,
        "repoCount": 0,
        "authSource": source,
        "ghAvailable": gh_available,
        "limit": None,
        "remaining": None,
        "notLoggedIn": False,
        "note": note,
        "noteError": "",
        "session": session,
    }


def _safe_rate(meta, response):
    limit, remaining = rate_of(response)
    if limit is not None:
        meta["limit"] = limit
    if remaining is not None:
        meta["remaining"] = remaining


def _run_poll():
    prepare_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--max-repos", type=int, default=30)
    parser.add_argument("--per-page", type=int, default=6)
    parser.add_argument("--host", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--fetch-bodies", action="store_true")
    parser.add_argument("--no-etag", action="store_true")
    parser.add_argument("--session", default="")
    args = parser.parse_args()
    args.max_repos = max(1, min(MAX_TOTAL_REPOS, args.max_repos))
    args.per_page = max(1, min(MAX_PER_PAGE, args.per_page))
    args.timeout = max(1, min(MAX_TIMEOUT, args.timeout))
    args.repo = normalize_repos(args.repo, MAX_EXTRA_REPOS)
    session = _safe_text(str(args.session or ""), 128) or ""
    output = BoundedOutput(sys.stdout)
    terminal_emitted = False
    try:
        host = canonical_api_host(args.host)
    except ValueError as error:
        meta = _meta(session, note=str(error))
        output.emit({"__meta": meta}, final=True)
        terminal_emitted = True
        output.flush()
        return 2
    web_origin = web_base(host)
    deadline = poll_deadline_seconds(args.timeout, args.max_repos, len(args.repo),
                                       args.per_page, args.fetch_bodies)
    try:
        signal.signal(signal.SIGALRM, _expire)
        signal.alarm(int(deadline))
    except (AttributeError, ValueError):
        pass
    gh_available = _trusted_gh() is not None
    explicit_token = bool(_clean_token(getattr(args, "token", "")))
    token, source = resolve_token(args, host)
    args.token = token
    state_path = etag_path()
    state = _new_state() if args.no_etag else read_state(state_path)
    namespace = etag_namespace(token, host)
    meta = _meta(session, source, gh_available, web_origin=web_origin)
    repos = []
    user_response = None
    try:
        login, owned, user_response = account_repos(args, host, token, args.timeout, state, namespace)
        meta["account"] = login
        _safe_rate(meta, user_response)
        if user_response is None:
            cache_map = state.get("cache")
            cached = cache_map.get(namespace, {}) if isinstance(cache_map, dict) else {}
            meta["limit"] = cached.get("limit") or None
            meta["remaining"] = cached.get("remaining") or None
        repos = owned
    except urllib.error.HTTPError as error:
        if error.code == 401 and source == "gh" and not explicit_token:
            invalidate_gh_token_cache()
            args.token = ""
            token, source = resolve_token(args, host, force_refresh=True)
            args.token = token
            namespace = etag_namespace(token, host)
            meta["authSource"] = source
            if token:
                try:
                    login, repos, user_response = account_repos(args, host, token, args.timeout, state, namespace)
                    meta["account"] = login
                    _safe_rate(meta, user_response)
                except urllib.error.HTTPError as retry_error:
                    meta["notLoggedIn"] = retry_error.code in (401, 403)
                    meta["note"] = http_error_message(retry_error)
                except (OSError, ValueError, urllib.error.URLError) as retry_error:
                    meta["note"] = "network error: {0}".format(retry_error)
            else:
                meta["notLoggedIn"] = True
                meta["note"] = http_error_message(error)
        else:
            meta["notLoggedIn"] = error.code in (401, 403)
            meta["note"] = http_error_message(error)
            if error.code == 401 and source == "gh":
                invalidate_gh_token_cache()
    except (OSError, ValueError, urllib.error.URLError) as error:
        meta["note"] = "network error: {0}".format(error)
    combined = normalize_repos(repos + args.repo, MAX_TOTAL_REPOS)
    if meta["notLoggedIn"]:
        combined = []
    repos = combined
    meta["repoCount"] = len(repos)
    if repos and not meta["notLoggedIn"]:
        if token:
            notifications, note_error, changed = fetch_notifications(
                args, host, state, namespace, return_changed=True)
            if note_error:
                meta["noteError"] = note_error
            try:
                if changed:
                    for note in notifications:
                        note["__notification"] = True
                        output.emit(note)
                else:
                    output.emit({"__notifications": "unchanged"})
            except OutputLimitExceeded:
                meta["note"] = "output limit exceeded"
            if meta["note"] != "output limit exceeded":
                results = []
                workers = min(MAX_WORKERS, len(repos))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(fetch_repo, args, host, repo, state, namespace): repo for repo in repos}
                    for future in as_completed(futures):
                        try:
                            results.append(future.result())
                        except Exception as error:
                            results.append({"repo": futures[future], "error": "network error: {0}".format(error)})
                order = {repo: index for index, repo in enumerate(repos)}
                results.sort(key=lambda item: order.get(item.get("repo"), 0))
                for result in results:
                    rate = result.pop("rate", None)
                    if rate:
                        if meta["limit"] is None:
                            meta["limit"] = rate.get("limit")
                        if rate.get("remaining") is not None:
                            meta["remaining"] = rate.get("remaining")
                    try:
                        output.emit(result)
                    except OutputLimitExceeded:
                        meta["note"] = "output limit exceeded"
                        break
        else:
            for repo in repos:
                try:
                    output.emit({"repo": repo, "error": "no credentials"})
                except OutputLimitExceeded:
                    meta["note"] = "output limit exceeded"
                    break
    elif not meta["notLoggedIn"] and not repos and token:
        meta["note"] = "no repositories found for {0}".format(meta["account"] or "account")
    prune_state(state, active_namespace=namespace)
    try:
        output.emit({"__meta": meta}, final=True)
        terminal_emitted = True
    except OutputLimitExceeded:
        compact = _meta(session, source, gh_available, "output limit exceeded", web_origin)
        try:
            output.emit({"__meta": compact}, final=True)
            terminal_emitted = True
        except OutputLimitExceeded:
            pass
    output.flush()
    write_state(state_path, state)
    try:
        signal.alarm(0)
    except (AttributeError, ValueError):
        pass
    return 0 if terminal_emitted else 1


def main():
    try:
        return _run_poll()
    except (BrokenPipeError, OSError, ValueError, TypeError, RecursionError):
        return 1
    except Exception:
        return 1
    finally:
        try:
            signal.alarm(0)
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
