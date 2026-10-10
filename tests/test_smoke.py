"""Exercise the standalone script over a local HTTP socket, without real DTK."""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.support import ROOT, fixture
from tests.test_dtk_client import TASK_ID, URL


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, *, status=200, success=True, error=None):
                body = json.dumps(
                    {
                        "success": success,
                        "data": data,
                        "error": error,
                        "meta": {"request_id": "local-smoke-request"},
                    }
                ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("POST", self.path, body))
                if self.headers.get("Authorization") != "Bearer dtk_local_test":
                    return self.reply(
                        None, status=401, success=False, error={"code": "UNAUTHENTICATED"}
                    )
                self.reply({"task_id": TASK_ID, "state": "queued"}, status=202)

            def do_GET(self):
                requests.append(("GET", self.path, None))
                self.reply({"state": "done", "data": fixture("video")})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def run_script(self, *, key="dtk_local_test", extra_args=()):
        env = {
            **os.environ,
            "DTK_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/proxy",
            "DTK_API_KEY": key,
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_dtk.py"), URL, *extra_args],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_script_uses_real_http_transport_and_proxy_prefix(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body["content_id"], "7660875690212492466")
        self.assertEqual(body["video_size_bytes"], 14784469)
        self.assertTrue(body["has_video"])
        self.assertTrue(body["has_cover"])
        self.assertTrue(body["has_thumbnail"])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[0][1].startswith("/proxy/api/v1/parse?"))
        self.assertEqual(self.requests[0][2]["include_raw"], True)
        self.assertTrue(self.requests[1][1].startswith(f"/proxy/api/v1/tasks/{TASK_ID}?"))
        self.assertNotIn("dtk_local_test", result.stdout + result.stderr)
        self.assertNotIn("https://cdn.example/", result.stdout + result.stderr)

    def test_script_failure_exit_code_and_safe_error_json(self):
        result = self.run_script(key="bad-key")
        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stderr)
        self.assertEqual(error["error"], "UNAUTHENTICATED")
        self.assertEqual(error["request_id"], "local-smoke-request")
        self.assertEqual(len(self.requests), 1)

    def test_script_rejects_invalid_timeout(self):
        result = self.run_script(extra_args=("--task-timeout", "0"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.requests, [])
