"""`python -m agilevow demo` fills an empty board through the API, by the rules, and never writes into a folder
that is not its own."""
import contextlib
import io
import json
import re
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

from agilevow import __main__ as cli, demo, server


def get(url):
    with urllib.request.urlopen(url, timeout=10) as reply:
        return json.load(reply)


class SeedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.server = server.make_server(self.root, 0, quiet=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_every_column_gets_a_card_and_each_got_there_by_the_rules(self):
        self.assertEqual(demo.seed(self.url, self.root), 11)
        snap = get(self.url + "/api/tasks")
        self.assertEqual(len(snap["boards"]), 1)
        self.assertEqual([p["title"] for p in snap["boards"][0]["principles"]], ["Evidence before claims", "Keep changes small", "Report as you work"])
        # the agents do what the third principle asks: every card they claimed says what they set out to do, in a comment
        for task in snap["tasks"]:
            if task["owner"]:
                said = [c for c in task.get("comments", []) if c.get("author") == "agent-1"]
                self.assertTrue(said and said[0]["body"].startswith("Starting:"), task["key"])
        self.assertEqual(len(snap["plans"]), 2)
        self.assertEqual(Counter(t["status"] for t in snap["tasks"]),
                         {"done": 1, "post_validation": 1, "review": 1, "doing": 1, "blocked": 1, "ready": 3, "backlog": 3})
        for task in snap["tasks"]:
            if task["status"] in ("review", "post_validation", "done"):
                # every criterion has evidence and is ticked: the server refused the move otherwise
                self.assertEqual(sorted(e["criterion"] for e in task["evidence"]), list(range(len(task["acceptance"]))), task["key"])
                self.assertTrue(all(c["checked"] for c in task["checklist"]), task["key"])
                self.assertEqual(task["owner"], "agent-1")
        doing = next(t for t in snap["tasks"] if t["status"] == "doing")
        self.assertEqual((doing["key"], len(doing["evidence"]), doing["owner"]), ("rate-limit", 1, "agent-1"))
        self.assertIn("Retry-After", doing["next_step"])
        # Done only through a validation report, filed by someone other than the worker
        done = next(t for t in snap["tasks"] if t["status"] == "done")
        events = get(self.url + "/api/tasks/item?" + urllib.parse.urlencode({"id": done["id"]}))["events"]
        self.assertIn("reviewer", {e.get("actor") for e in events})
        verdict = done["validations"][-1]
        self.assertEqual({p["principle_key"]: p["passed"] for p in verdict["principles"]},
                         {"evidence-first": True, "small-changes": True, "report-as-you-work": True})
        # every piece of evidence is a file a reviewer can open (so the board links it), marked as sample data
        refs = {e["ref"] for t in snap["tasks"] for e in t["evidence"]}
        self.assertEqual(refs, set(demo.FILES))
        for ref in refs:
            self.assertTrue((self.root / ref).read_text().startswith("Sample evidence"), ref)
        # the lease token behind the Doing card is kept owner-only
        leases = self.root / ".agilevow" / "leases.json"
        self.assertEqual(stat.S_IMODE(leases.stat().st_mode), 0o600)
        self.assertIn(doing["id"], json.loads(leases.read_text()))

    def test_an_existing_file_is_never_overwritten(self):
        mine = self.root / "evidence" / "routes.txt"
        mine.parent.mkdir()
        mine.write_text("mine\n")
        demo.seed(self.url, self.root)
        self.assertEqual(mine.read_text(), "mine\n")


class DemoCommandTests(unittest.TestCase):
    def run_demo(self, *argv):
        """Run the command in a thread until it is serving, read the board, then stop it."""
        out, err, stop, result = io.StringIO(), io.StringIO(), threading.Event(), {}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            thread = threading.Thread(target=lambda: result.setdefault("code", demo.main(list(argv), stop=stop)))
            thread.start()
            deadline = time.time() + 30
            while "Press Ctrl+C" not in out.getvalue() and thread.is_alive() and time.time() < deadline:
                time.sleep(0.05)
            found = re.search(r"http://127\.0\.0\.1:\d+/", out.getvalue())
            snapshot = get(found.group(0) + "api/tasks") if found and thread.is_alive() else None
            stop.set()
            thread.join(15)
        self.assertFalse(thread.is_alive(), "the demo did not stop")
        return result.get("code"), out.getvalue(), err.getvalue(), snapshot

    def test_fills_a_new_folder_once_and_then_only_serves_it(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "demo"
            code, out, _, snap = self.run_demo("--dir", str(target), "--port", "0", "--no-open")
            self.assertEqual(code, 0)
            self.assertIn("11 sample tasks", out)
            self.assertEqual(len(snap["tasks"]), 11)
            code, out, _, snap = self.run_demo("--dir", str(target), "--port", "0", "--no-open")
            self.assertEqual(code, 0)
            self.assertIn("already has boards", out)
            self.assertEqual(len(snap["tasks"]), 11, "a second run must not add the sample data again")

    def test_a_folder_that_is_not_an_agilevow_folder_is_refused_and_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "keep.txt").write_text("someone's work\n")
            code, _, err, snap = self.run_demo("--dir", folder, "--port", "0", "--no-open")
            self.assertEqual(code, 2)
            self.assertIn("not an AgileVow folder", err)
            self.assertIsNone(snap)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["keep.txt"])
            self.assertEqual((Path(folder) / "keep.txt").read_text(), "someone's work\n")

    def test_a_port_that_is_taken_is_a_message_not_a_traceback(self):
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with tempfile.TemporaryDirectory() as folder:
            code, _, err, _ = self.run_demo("--dir", str(Path(folder) / "demo"), "--port", str(busy.getsockname()[1]), "--no-open")
        self.assertEqual(code, 2)
        self.assertIn(str(busy.getsockname()[1]), err)
        self.assertNotIn("Traceback", err)

    def test_the_page_is_opened_unless_asked_not_to(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("agilevow.demo.webbrowser.open") as opened:
            self.run_demo("--dir", str(Path(folder) / "a"), "--port", "0", "--no-open")
            opened.assert_not_called()
            _, out, _, _ = self.run_demo("--dir", str(Path(folder) / "b"), "--port", "0")
            opened.assert_called_once_with(re.search(r"http://127\.0\.0\.1:\d+/", out).group(0))

    def test_the_command_is_listed(self):
        self.assertIn("demo", cli.usage())


if __name__ == "__main__":
    unittest.main()
