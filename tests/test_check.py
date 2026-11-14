"""Tests for scripts/check.py — run with: python3 -m unittest discover -s tests -v

A local HTTP server stands in for the live sites and for healthchecks, so the
tests need no network and can make every branch red on purpose.
"""
import http.server
import importlib.util
import os
import pathlib
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

PINGS = []  # (path, body) received by the fake healthchecks endpoint


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, b"<html><title>Snake-Oil-or-Gold Check</title></html>")
        elif self.path == "/paused":
            self._send(503, b"The deployment is currently unavailable DEPLOYMENT_PAUSED")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        PINGS.append((self.path, self.rfile.read(length).decode()))
        self._send(200, b"OK")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        PINGS.clear()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def group(self, body):
        path = pathlib.Path(self.tmp.name) / "g.toml"
        path.write_text('group = "test-group"\n' + body)
        return path

    def run_check(self, path, ping=True, probe=False):
        ping_url = f"{self.base}/ping/abc" if ping else None
        return check.main_for(path, ping_url=ping_url, probe=probe, tls=False)

    # --- content assertion -------------------------------------------------

    def test_pass_sends_success_ping(self):
        g = self.group(f'[[assert]]\nname = "home"\nurl = "{self.base}/ok"\nexpect = ["Snake-Oil-or-Gold Check"]\n')
        self.assertEqual(self.run_check(g), 0)
        self.assertEqual([p for p, _ in PINGS], ["/ping/abc"])

    def test_missing_text_sends_fail_ping_with_name_and_url(self):
        g = self.group(f'[[assert]]\nname = "home"\nurl = "{self.base}/ok"\nexpect = ["Something else"]\n')
        self.assertEqual(self.run_check(g), 1)
        self.assertEqual(len(PINGS), 1)
        path, body = PINGS[0]
        self.assertEqual(path, "/ping/abc/fail")
        self.assertIn("home", body)
        self.assertIn(f"{self.base}/ok", body)

    def test_status_code_alone_does_not_pass(self):
        # AD-65: content, not status code. A 503 page fails on its content.
        g = self.group(f'[[assert]]\nname = "paused"\nurl = "{self.base}/paused"\nexpect = ["Snake-Oil"]\n')
        self.assertEqual(self.run_check(g), 1)
        self.assertTrue(PINGS[0][0].endswith("/fail"))

    def test_fail_body_never_carries_response_body(self):
        # 1a: nothing but the public URL and the assertion name leaves the run.
        g = self.group(f'[[assert]]\nname = "paused"\nurl = "{self.base}/paused"\nexpect = ["Snake-Oil"]\n')
        self.run_check(g)
        self.assertNotIn("DEPLOYMENT_PAUSED", PINGS[0][1])

    def test_unreachable_is_a_failure(self):
        g = self.group('[[assert]]\nname = "dead"\nurl = "http://127.0.0.1:9/"\nexpect = ["x"]\n')
        self.assertEqual(self.run_check(g), 1)
        self.assertTrue(PINGS[0][0].endswith("/fail"))

    # --- an empty group never reports green (#1954) ------------------------

    def test_zero_active_assertions_refuses_and_never_pings(self):
        g = self.group(
            f'[[assert]]\nname = "parked"\nurl = "{self.base}/ok"\nexpect = ["x"]\n'
            'active = false\nreason = "parked by the Founder"\n'
        )
        self.assertEqual(self.run_check(g), 2)
        self.assertEqual(PINGS, [])

    def test_group_without_any_assertion_refuses(self):
        g = self.group("")
        self.assertEqual(self.run_check(g), 2)
        self.assertEqual(PINGS, [])

    # --- configuration is validated, never guessed -------------------------

    def test_inactive_without_reason_is_a_config_error(self):
        g = self.group(f'[[assert]]\nname = "p"\nurl = "{self.base}/ok"\nexpect = ["x"]\nactive = false\n')
        self.assertEqual(self.run_check(g), 2)

    def test_active_without_expect_is_a_config_error(self):
        g = self.group(f'[[assert]]\nname = "p"\nurl = "{self.base}/ok"\n')
        self.assertEqual(self.run_check(g), 2)

    def test_plain_http_is_refused_for_live_groups(self):
        os.environ["LIVE_CHECKS_REQUIRE_HTTPS"] = "1"
        try:
            g = self.group(f'[[assert]]\nname = "p"\nurl = "{self.base}/ok"\nexpect = ["x"]\n')
            self.assertEqual(self.run_check(g), 2)
        finally:
            del os.environ["LIVE_CHECKS_REQUIRE_HTTPS"]

    # --- probe and no-ping modes -------------------------------------------

    def test_probe_turns_a_passing_group_red(self):
        g = self.group(f'[[assert]]\nname = "home"\nurl = "{self.base}/ok"\nexpect = ["Snake-Oil-or-Gold Check"]\n')
        self.assertEqual(self.run_check(g, probe=True), 1)
        self.assertTrue(PINGS[0][0].endswith("/fail"))
        self.assertIn("probe", PINGS[0][1])

    def test_without_ping_url_result_still_decides_exit(self):
        g = self.group(f'[[assert]]\nname = "home"\nurl = "{self.base}/ok"\nexpect = ["nope"]\n')
        self.assertEqual(self.run_check(g, ping=False), 1)
        self.assertEqual(PINGS, [])


class ConfigFilesTests(unittest.TestCase):
    """Every shipped group file parses, and every inactive entry names a reason."""

    def test_shipped_groups_are_valid(self):
        for path in sorted((ROOT / "checks").glob("*.toml")):
            with self.subTest(group=path.name):
                group = check.load_group(path)
                self.assertEqual(group["group"], path.stem)
                for a in group["assert"]:
                    self.assertTrue(a["url"].startswith("https://"), a["name"])


if __name__ == "__main__":
    unittest.main()
