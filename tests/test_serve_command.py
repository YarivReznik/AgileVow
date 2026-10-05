"""`python -m agilevow serve` explains itself when it cannot start."""
import contextlib
import io
from pathlib import Path
import socket
import tempfile
import unittest

from agilevow import server


class ServeCommandTests(unittest.TestCase):
    def run_serve(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = server.main(list(argv))
        return code, err.getvalue()

    def test_a_port_that_is_taken_is_a_message_not_a_traceback(self):
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with tempfile.TemporaryDirectory() as folder:
            code, message = self.run_serve("--root", folder, "--port", str(port))
        self.assertEqual(code, 2)
        self.assertIn(str(port), message)
        self.assertNotIn("Traceback", message)

    def test_a_project_folder_that_does_not_exist_is_refused_and_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "typo"
            code, message = self.run_serve("--root", str(missing), "--port", "0")
            self.assertEqual(code, 2)
            self.assertIn("does not exist", message)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
