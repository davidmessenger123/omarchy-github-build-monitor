import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "github-builds.py"
SPEC = importlib.util.spec_from_file_location("github_builds_under_test", MODULE_PATH)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class Response:
    def __init__(self, payload=None, url="https://api.github.com/test", headers=None, status=200):
        self.payload = payload
        self.url = url
        self.headers = headers or {}
        self.status = status
        self.closed = False

    def read(self, size=-1):
        if self.payload is None:
            return b""
        if size < 0:
            size = len(self.payload)
        result = self.payload[:size]
        self.payload = self.payload[size:]
        return result

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class MonitorTests(unittest.TestCase):
    def test_host_canonicalization_and_validation(self):
        self.assertEqual(BUILD.canonical_api_host("API.GITHUB.COM."), "api.github.com")
        self.assertEqual(BUILD.canonical_api_host("https://API.GITHUB.COM/"), "api.github.com")
        self.assertEqual(BUILD.canonical_api_host("ghe.example.test:8443"), "ghe.example.test:8443")
        self.assertEqual(BUILD.canonical_api_host("https://api.github.com:443/"), "api.github.com")
        with self.assertRaises(ValueError):
            BUILD.canonical_api_host("https://127.0.0.1/")
        for value in (
            "https://user:secret@api.github.com",
            "api.github.com/path",
            "http://api.github.com",
            "api.github.com?x=1",
            "api.github.com:bad",
            "api.github.com\\evil",
            "api.github.com\n",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    BUILD.canonical_api_host(value)

    def test_environment_token_precedence(self):
        args = SimpleNamespace(token="")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "environment-token"}):
            self.assertEqual(BUILD.resolve_token(args, "api.github.com"), ("environment-token", "env"))
            args.token = "argument-token"
            self.assertEqual(BUILD.resolve_token(args, "api.github.com"), ("argument-token", "token"))

    def test_redirect_origin_blocks_cross_origin_authorization(self):
        request = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": "Bearer secret"},
        )
        handler = BUILD.SafeRedirectHandler(BUILD.host_origin("api.github.com"))
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(
                request,
                io.BytesIO(),
                302,
                "Found",
                {"Location": "https://evil.example/steal"},
                "https://evil.example/steal",
            )
        redirected = handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            {"Location": "https://api.github.com/login"},
            "https://api.github.com/login",
        )
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")

    def test_absolute_fetch_rejects_other_origin_before_open(self):
        original = BUILD._open_response
        called = []

        def should_not_open(*args, **kwargs):
            called.append(True)
            raise AssertionError("network opened")

        BUILD._open_response = should_not_open
        try:
            with self.assertRaises(ValueError):
                BUILD.http_absolute(
                    "https://user:secret@evil.example/subject",
                    "token",
                    1,
                    allowed_host="api.github.com",
                )
        finally:
            BUILD._open_response = original
        self.assertEqual(called, [])

    def test_cache_schema_namespace_and_no_etag_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "etags.json")
            oversized = "x" * (BUILD.ETAG_MAX_VALUE_BYTES + 1)
            state = BUILD._new_state()
            state["etags"] = {"a" * 32: oversized, "b" * 32: '"ok"'}
            state["updated"] = {"b" * 32: BUILD.time.time()}
            state["cache"] = {}
            self.assertTrue(BUILD.write_state(path, state))
            loaded = BUILD.read_state(path)
            self.assertNotIn("a" * 32, loaded["etags"])
            self.assertEqual(loaded["etags"]["b" * 32], '"ok"')
            self.assertNotEqual(
                BUILD.etag_namespace("token-a", "api.github.com"),
                BUILD.etag_namespace("token-b", "api.github.com"),
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[]")
            os.chmod(path, 0o600)
            self.assertEqual(BUILD.read_state(path), BUILD._new_state())
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"version": 2, "etags": {"x": {"value": []}}}))
            os.chmod(path, 0o600)
            self.assertEqual(BUILD.read_state(path)["etags"], {})
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"version": 2, "etags": {"x": "y"}, "updated": {"x": float("nan")}}))
            os.chmod(path, 0o600)
            self.assertEqual(BUILD.read_state(path)["etags"], {})

    def test_malformed_api_payloads_are_rejected(self):
        args = SimpleNamespace(max_repos=5, per_page=6, no_etag=True, token="token", timeout=1)
        response = Response(b"{}", headers={})

        def malformed_user(host, path, token, timeout, **kwargs):
            if path == "/user":
                return {"login": "me"}, response
            return {"bad": True}, response

        original = BUILD.http_json
        BUILD.http_json = malformed_user
        try:
            with self.assertRaises(ValueError):
                BUILD.account_repos(args, "api.github.com", "token", 1)
        finally:
            BUILD.http_json = original

        BUILD.http_json = lambda *args, **kwargs: ({"workflow_runs": "bad"}, response)
        try:
            result = BUILD.fetch_repo(args, "api.github.com", "owner/name", BUILD._new_state(), "a" * 32)
        finally:
            BUILD.http_json = original
        self.assertEqual(result["error"], "malformed workflow response")

    def test_cache_prunes_namespaces_and_etag_count(self):
        state = BUILD._new_state()
        now = BUILD.time.time()
        state["etags"] = {"{0}|key".format(index): '"v"' for index in range(2100)}
        state["updated"] = {key: now for key in state["etags"]}
        state["cache"] = {
            "{0:032x}".format(index): {"account": "user", "repos": [], "updated": now}
            for index in range(12)
        }
        BUILD.prune_state(state, now=now, active_namespace="00000000000000000000000000000000")
        self.assertLessEqual(len(state["etags"]), BUILD.CACHE_MAX_ETAGS)
        self.assertLessEqual(len(state["cache"]), BUILD.CACHE_MAX_NAMESPACES)
        self.assertIn("00000000000000000000000000000000", state["cache"])

    def test_caps_and_work_deadline(self):
        repos = ["owner/repo{0}".format(i) for i in range(200)]
        self.assertLessEqual(len(BUILD.normalize_repos(repos, BUILD.MAX_TOTAL_REPOS)), 100)
        self.assertGreater(
            BUILD.poll_deadline_seconds(12, 30, extras=90),
            BUILD.poll_deadline_seconds(12, 30, extras=0),
        )
        output = BUILD.BoundedOutput(io.StringIO(), max_bytes=24)
        with self.assertRaises(BUILD.OutputLimitExceeded):
            output.emit({"repo": "owner/name", "runs": [{"name": "x" * 100}]})

    def test_not_modified_transition_preserves_cached_repositories(self):
        args = SimpleNamespace(max_repos=2, no_etag=False, token="token", timeout=1)
        state = BUILD._new_state()
        namespace = "a" * 32
        state["etags"] = {_BUILD_key(namespace, "/user"): '"user"', _BUILD_key(namespace, "/user/repos?affiliation=owner&sort=updated&per_page=100&page=1"): '"repos"'}
        state["updated"] = {key: 1 for key in state["etags"]}
        state["cache"] = {namespace: {"account": "me", "repos": ["owner/one", "owner/two"], "updated": 1}}
        calls = []

        def not_modified(host, path, token, timeout, **kwargs):
            calls.append(path)
            return None, None

        original = BUILD.http_json
        BUILD.http_json = not_modified
        try:
            login, repos, response = BUILD.account_repos(args, "api.github.com", "token", 1, state, namespace)
        finally:
            BUILD.http_json = original
        self.assertEqual(login, "me")
        self.assertEqual(repos, ["owner/one", "owner/two"])
        self.assertEqual(calls, ["/user", "/user/repos?affiliation=owner&sort=updated&per_page=100&page=1"])

    def test_environment_token_is_supported_for_enterprise(self):
        args = SimpleNamespace(token="")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "environment-token"}), \
             mock.patch.object(BUILD, "gh_token") as gh:
            self.assertEqual(BUILD.resolve_token(args, "ghe.example.test"), ("environment-token", "env"))
            gh.assert_not_called()
            args.token = "enterprise-token"
            self.assertEqual(BUILD.resolve_token(args, "ghe.example.test"), ("enterprise-token", "token"))

    def test_enterprise_web_origin_is_used_for_links(self):
        self.assertEqual(BUILD.web_base("api.ghe.example.test"), "ghe.example.test")
        note = {"repository": {"full_name": "owner/repo"}}
        self.assertEqual(
            BUILD.notification_html("api.ghe.example.test", note),
            "https://ghe.example.test/owner/repo",
        )
        note = {"subject": {"url": "https://api.github.com/repos/owner/repo/issues/1"}}
        self.assertEqual(
            BUILD.notification_html("api.github.com", note),
            "https://github.com/owner/repo",
        )

    def test_workflow_schema_rejects_nonfinite_and_cross_origin_links(self):
        args = SimpleNamespace(max_repos=5, per_page=6, no_etag=True, token="token", timeout=1)
        response = Response(b"{}", headers={})
        original = BUILD.http_json
        BUILD.http_json = lambda *args, **kwargs: ({
            "workflow_runs": [{
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://evil.example/owner/repo/actions/runs/1",
            }]
        }, response)
        try:
            result = BUILD.fetch_repo(args, "api.github.com", "owner/repo")
        finally:
            BUILD.http_json = original
        self.assertEqual(result["error"], "malformed workflow response")
        self.assertEqual(BUILD.trim_run({"status": "completed", "conclusion": "success", "run_number": float("nan")}), {})

    def test_protected_state_reader_rejects_symlink_and_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.chmod(path, 0o600)
            link = os.path.join(directory, "link.json")
            os.symlink(path, link)
            with self.assertRaises(BUILD.StateSecurityError):
                BUILD._secure_read(link, 1024)
            os.unlink(path)
            os.mkfifo(path)
            with self.assertRaises(BUILD.StateSecurityError):
                BUILD._secure_read(path, 1024)

    def test_unexpected_failure_without_output_is_nonzero(self):
        output = io.StringIO()
        with mock.patch.object(BUILD, "prepare_environment", side_effect=RuntimeError("boom")), \
             mock.patch.object(BUILD.sys, "stdout", output):
            self.assertEqual(BUILD.main(), 1)
        self.assertEqual(output.getvalue(), "")

    def test_output_cap_is_checked_before_json_buffering(self):
        output = BUILD.BoundedOutput(io.StringIO(), max_bytes=32)
        with self.assertRaises(BUILD.OutputLimitExceeded):
            output.emit({"value": "x" * 100})


def _BUILD_key(namespace, path):
    return namespace + "|" + path


class QmlContract(unittest.TestCase):
    def test_service_owns_the_poller(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "Service.qml").read_text(encoding="utf-8")
        widget = (root / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("github-builds.py", service)
        self.assertIn("Model.deriveState", service)
        self.assertIn("bar.shell.serviceFor(root.moduleName)", widget)
        self.assertNotIn("Process {", widget)
        self.assertNotIn("pollTimer", widget)

if __name__ == "__main__":
    unittest.main()
