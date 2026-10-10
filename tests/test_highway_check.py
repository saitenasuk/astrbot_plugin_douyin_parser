import json
import shutil
import socket
import subprocess
import unittest

from tests.support import ROOT


@unittest.skipUnless(shutil.which("node"), "Node.js is optional for the diagnostic script")
class HighwayCheckTests(unittest.TestCase):
    def check(self, *args):
        return subprocess.run(
            [shutil.which("node"), str(ROOT / "scripts" / "check_highway.cjs"), *args],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

    def test_direct_tcp_connect_without_sending_qq_payload(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            result = self.check("127.0.0.1", str(server.getsockname()[1]), "1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "connected")

    def test_connection_failure_is_nonzero_exit(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        result = self.check("127.0.0.1", str(port), "1000")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")

    def test_invalid_arguments_do_not_open_socket(self):
        for args in (("localhost", "0"), ("localhost", "65536"), ("localhost", "80", "-1")):
            with self.subTest(args=args):
                result = self.check(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)
