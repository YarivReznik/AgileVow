"""The agent side against a live server: leases on disk, the principles gate, and the whole loop to Done."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from agilevow import client, manager, review
from agilevow.server import make_server

PRINCIPLES = [{"key": "terse", "title": "Terse", "content": "Say it in one line."}]
PLAN = Path(__file__).resolve().parent.parent / "examples" / "plan.json"


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "notes.md").write_text("Observed result\n")
        self.server = make_server(self.root, port=0, quiet=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)  # runs first: cleanups are last-in, first-out
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.leases = self.root / "leases.json"
        self.board = client.Board(actor="worker", url=self.url, leases=self.leases)

    def fixture(self, principles=()):
        board = self.board.create_board("demo", "Demo board", principles=principles)
        self.board.import_plan(board, json.loads(PLAN.read_text()))
        return board

    def post_validation_card(self, principles=()):
        self.fixture(principles)
        card = self.board.task("write-readme")
        card = self.board.claim(card, self.board.context(card)["principles_sha256"] if principles else None)
        card = self.board.set_evidence(card, (0, "notes.md", "Read it"), (1, "notes.md", "Ran every command"))
        card = self.board.check(card)
        card = self.board.action(card, "transition", status="review")
        return self.board.action(card, "transition", status="post_validation")

    def cli(self, *argv, url=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"AGILEVOW_URL": url or self.url, "AGILEVOW_ACTOR": "worker", "AGILEVOW_LEASES": str(self.leases)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_lease_file_is_owner_only_and_forgets_released_tasks(self):
        self.fixture()
        task = self.board.task("write-readme")
        self.board.claim(task)
        self.assertEqual(stat.S_IMODE(os.stat(self.leases).st_mode), 0o600)
        self.assertIn(task["id"], json.loads(self.leases.read_text()))
        self.board.action(task, "release", note="Stepping away")
        self.assertEqual(json.loads(self.leases.read_text()), {})

    def test_claim_does_not_acknowledge_the_principles_for_you(self):
        self.fixture(PRINCIPLES)
        task = self.board.task("write-readme")
        with self.assertRaises(client.BoardError) as refused:
            self.board.claim(task)
        self.assertIn("409", str(refused.exception))
        self.assertIn("principles_sha256", str(refused.exception))
        self.assertEqual(self.board.fresh(task)["status"], "ready")
        context = self.board.context(task)
        self.assertIn("Say it in one line.", context["text"])
        self.assertEqual(self.board.claim(task, context["principles_sha256"])["status"], "doing")

    def test_cli_context_prints_the_hash_and_claim_demands_it(self):
        self.fixture(PRINCIPLES)
        code, out, _ = self.cli("context", "write-readme")
        self.assertEqual(code, 0)
        self.assertIn("Say it in one line.", out)
        digest = out.strip().splitlines()[-1].split(": ")[1]
        self.assertEqual(len(digest), 64)
        code, _, err = self.cli("claim", "write-readme")
        self.assertEqual(code, 1)
        self.assertIn("409", err)
        code, out, _ = self.cli("claim", "write-readme", "--ack-principles", digest)
        self.assertEqual((code, "claimed" in out), (0, True))

    def test_cli_says_plainly_when_no_server_is_running(self):
        code, _, err = self.cli("boards", url="http://127.0.0.1:1")
        self.assertEqual(code, 1)
        self.assertIn("python -m agilevow serve", err)

    def test_a_task_key_used_in_two_plans_must_be_named_precisely(self):
        board = self.board.create_board("demo", "Demo board")
        plan = json.loads(PLAN.read_text())
        self.board.import_plan(board, plan)
        self.board.import_plan(board, dict(plan, source_key="second-launch"))
        with self.assertRaises(client.BoardError) as ambiguous:
            self.board.task("write-readme")
        self.assertIn("--plan", str(ambiguous.exception))
        self.assertEqual(self.board.task("write-readme", "second-launch")["key"], "write-readme")

    def test_cli_release_needs_a_note_and_hands_the_card_back(self):
        self.fixture()
        self.assertEqual(self.cli("claim", "write-readme")[0], 0)
        with self.assertRaises(SystemExit) as refused:
            self.cli("release", "write-readme")  # --note is required, as it is on the server
        self.assertEqual(refused.exception.code, 2)
        code, out, _ = self.cli("release", "write-readme", "--note", "Stepping away")
        self.assertEqual(code, 0)
        self.assertIn("ready", out)
        self.assertEqual(json.loads(self.leases.read_text()), {})

    def test_cli_renew_takes_a_length(self):
        self.fixture()
        self.cli("claim", "write-readme", "--seconds", "120")
        before = self.board.fresh(self.board.task("write-readme"))["claim_expires_at"]
        self.assertEqual(self.cli("renew", "write-readme", "--seconds", "1800")[0], 0)
        after = self.board.fresh(self.board.task("write-readme"))["claim_expires_at"]
        self.assertGreater(after - before, 1000)

    def test_cli_check_ticks_only_what_has_evidence_and_keeps_earlier_ticks(self):
        self.fixture()
        self.cli("claim", "write-readme")
        code, _, err = self.cli("check", "write-readme")
        self.assertEqual(code, 1)
        self.assertIn("evidence", err)  # a tick without evidence is refused by the server, and the message says so
        self.cli("evidence", "write-readme", "0", "notes.md", "--note", "Read it")
        self.assertEqual(self.cli("check", "write-readme", "0")[0], 0)
        ticks = lambda: [c["checked"] for c in self.board.fresh(self.board.task("write-readme"))["checklist"]]
        self.assertEqual(ticks(), [True, False])
        self.cli("evidence", "write-readme", "1", "notes.md", "--note", "Ran every command")
        self.assertEqual(self.cli("check", "write-readme", "1")[0], 0)
        self.assertEqual(ticks(), [True, True])  # ticking criterion 1 did not un-tick criterion 0

    def test_cli_names_the_file_it_cannot_use(self):
        self.fixture()
        code, _, err = self.cli("import", str(self.root / "missing.json"), "--board", "demo")
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        broken = self.root / "broken.json"
        broken.write_text("{not json")
        code, _, err = self.cli("import", str(broken), "--board", "demo")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        self.assertNotIn("Traceback", err)

    def test_create_board_counts_principles_in_the_singular(self):
        one = self.root / "one.json"
        one.write_text(json.dumps(PRINCIPLES))
        _, out, _ = self.cli("create-board", "solo", "Solo", "--principles", str(one))
        self.assertIn("(1 principle)", out)

    def test_manager_takes_a_board_key_as_well_as_an_id(self):
        board = self.fixture()
        for ref in ("demo", board["id"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(manager.main(["--url", self.url, "--board", ref]), 0)
            self.assertIn("ready_queue", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(manager.main(["--url", self.url, "--board", "nope"]), 1)
        self.assertIn("demo", err.getvalue())  # it lists the boards that do exist

    def test_review_explains_instead_of_crashing(self):
        card = self.post_validation_card()
        report = self.root / "report.json"
        args = ["--task", card["id"], "--report", str(report), "--url", self.url]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.main(["prepare", *args]), None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(review.main(["prepare", *args]), 1)  # the report file is already there
        self.assertIn("already exists", err.getvalue())
        report.unlink()
        ready = self.board.task("cut-release")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(review.main(["prepare", "--task", ready["id"], "--report", str(report), "--url", self.url]), 1)
        self.assertIn("Post validation", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_the_whole_loop_to_done_has_no_shortcut(self):
        self.fixture(PRINCIPLES)
        first, second = self.board.task("write-readme"), self.board.task("cut-release")
        digest = self.board.context(first)["principles_sha256"]
        self.assertEqual(second["status"], "backlog")  # waits on the first

        first = self.board.claim(first, digest)
        first = self.board.set_evidence(first, (0, "notes.md", "Read it"), (1, "notes.md", "Ran every command"))
        first = self.board.update(first, checklist_checks=[{"criterion": 0, "checked": True}, {"criterion": 1, "checked": True}])
        first = self.board.action(first, "transition", status="review")
        with self.assertRaises(client.BoardError):  # no transition reaches Done
            self.board.action(first, "transition", status="done")
        first = self.board.action(first, "transition", status="post_validation")

        report = self.root / "report.json"
        args = ["--task", first["id"], "--report", str(report), "--url", self.url]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.main(["prepare", *args]), None)
        filled = json.loads(report.read_text())
        filled.update(reviewer="qa", summary="Inspected the goals and the principle")
        for row in filled["checks"] + filled["principles"]:
            row.update(result="pass", method="Read notes.md", observations="It matches what the task asked for",
                       evidence=["notes.md"])
        report.write_text(json.dumps(filled))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            review.main(["submit", *args])
        self.assertEqual(json.loads(out.getvalue()), {"task": first["id"], "status": "done"})

        self.assertEqual(self.board.fresh(first)["status"], "done")
        self.assertEqual(self.board.action(second, "transition", status="ready")["status"], "ready")  # its dependency is done


if __name__ == "__main__":
    unittest.main()
