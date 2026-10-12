"""The change marker the board page polls (`GET /api/tasks/version`) moves on every write and on nothing else."""
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from agilevow import board as task_board, server
from agilevow.client import Board

PLAN = {"source_key": "p", "title": "P", "sources": [],
        "tasks": [{"key": "a", "title": "A", "outcome": "o", "acceptance": ["c"], "status": "ready"}]}


class ChangeMarkerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.server = server.make_server(self.root, 0, quiet=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = Board(actor="worker", url=self.url, leases=self.root / "leases.json")

    def version(self):
        with urllib.request.urlopen(self.url + "/api/tasks/version", timeout=10) as reply:
            return json.load(reply)["version"]

    def test_every_kind_of_write_moves_it(self):
        seen = [self.version()]
        board = self.client.create_board("k", "K")
        seen.append(self.version())
        self.client.import_plan(board, PLAN)
        seen.append(self.version())
        task = self.client.task("a")
        self.client.comment(task, "note", "hello")
        seen.append(self.version())
        task = self.client.claim(task)
        seen.append(self.version())
        self.client.set_evidence(task, (0, "notes.md", "observed"))
        seen.append(self.version())
        self.assertEqual(seen, sorted(set(seen)), f"each write must raise the marker: {seen}")

    def test_reads_and_writes_that_change_nothing_leave_it_alone(self):
        board = self.client.create_board("k", "K")
        self.client.import_plan(board, PLAN)
        before = self.version()
        self.client.snapshot()
        self.client.task("a")
        self.client.import_plan(board, PLAN)             # the same plan again is a no-op
        self.client.create_board("k", "K")               # so is the same board
        self.assertEqual(self.version(), before)

    def test_it_survives_event_pruning(self):
        """Only the newest 500 events per card are kept; the marker comes from ids that are never reused."""
        board = self.client.create_board("k", "K")
        self.client.import_plan(board, PLAN)
        task = self.client.task("a")
        last = self.version()
        for i in range(520):
            task = self.client.update(task, next_step=f"step {i}")
        self.assertGreater(self.version(), last + 500)

    def test_the_board_method_matches_the_route(self):
        self.client.create_board("k", "K")
        self.assertEqual(task_board.TaskBoard(self.root).change_marker(), self.version())


if __name__ == "__main__":
    unittest.main()
