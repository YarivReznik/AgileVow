"""Exercise the real HTTP boundary (server.py -> api.py -> board.py) with isolated durable data."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

from agilevow.server import make_server


class TaskHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "docs").mkdir()
        (cls.root / "docs/brief.md").write_text("One reproducible result.\n")
        cls.server = make_server(cls.root, port=0, link_kinds=("pr",), quiet=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def call(self, path, payload=None, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        req = Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as response:
            return response.code, response.read(), response.headers

    def ok(self, path, payload=None):
        code, body, _ = self.call(path, payload)
        self.assertEqual(code, 200, body)
        return json.loads(body)

    def fixture(self, scope="shared-area", principles=()):
        board = self.ok("/api/boards/create", {"actor": "tester", "source_key": uuid.uuid4().hex,
                        "title": "Test board", "description": "Isolated verification", "scope": scope,
                        "principles": list(principles)})["board"]
        body = {"actor": "tester", "board_id": board["id"], "source_key": "same-mini-project-key",
                "title": "A mini project", "description": "Deliver a result", "scope": scope,
                "kind": "code", "sources": ["docs/brief.md"],
                "tasks": [{"key": "one", "title": "Produce evidence", "outcome": "A verifiable change",
                           "acceptance": ["Result is readable"], "status": "ready"}]}
        result = self.ok("/api/plans/import", body)
        return board, result["plan"], result["tasks"][0], body

    def test_many_boards_import_replay_and_conflict(self):
        a, plan, task, body = self.fixture("area-a")
        b, _, other, _ = self.fixture("area-b")
        repeated = self.ok("/api/plans/import", body)
        self.assertFalse(repeated["imported"])
        self.assertEqual(repeated["tasks"][0]["id"], task["id"])
        code, _, _ = self.call("/api/plans/import", {**body, "title": "Changed behind your back"})
        self.assertEqual(code, 409)
        filtered = self.ok("/api/tasks?board=" + a["id"])
        self.assertEqual([t["id"] for t in filtered["tasks"]], [task["id"]])
        self.assertNotIn(other["id"], [t["id"] for t in filtered["tasks"]])
        code, _, _ = self.call("/api/tasks?board=" + b["id"] + "&plan=" + plan["id"])
        self.assertEqual(code, 400)

    def test_claim_work_evidence_and_completion_through_http(self):
        _, _, task, _ = self.fixture()
        result = self.ok("/api/tasks/action", {"op": "claim", "id": task["id"], "revision": task["revision"], "actor": "worker"})
        task, lease = result["task"], result["lease"]
        code, _, _ = self.call("/api/tasks/action", {"op": "claim", "id": task["id"], "revision": task["revision"], "actor": "other"})
        self.assertEqual(code, 409)
        code, _, _ = self.call("/api/tasks/update", {"id": task["id"], "revision": task["revision"], "actor": "worker", "lease": "wrong", "next_step": "Overwrite"})
        self.assertEqual(code, 409)
        task = self.ok("/api/tasks/update", {"id": task["id"], "revision": task["revision"], "actor": "worker", "lease": lease,
                       "evidence": [{"criterion": 0, "ref": "docs/brief.md", "note": "Read the retained output"}], "checklist_checks":[{"criterion":0,"checked":True}]})["task"]
        task = self.ok("/api/tasks/action", {"op": "transition", "status": "review", "id": task["id"], "revision": task["revision"], "actor": "worker", "lease": lease})["task"]
        task = self.ok("/api/tasks/action", {"op": "transition", "status": "post_validation", "id": task["id"], "revision": task["revision"], "actor": "worker"})["task"]
        task = self.ok("/api/tasks/action", {"op":"validate", "id":task["id"],"revision":task["revision"],"actor":"reviewer","validation":{"result":"pass","summary":"Read and compared output","principles":[],"checks":[{"criterion":0,"passed":True,"observed":"Readable output","ref":"docs/brief.md"}]}})["task"]
        self.assertEqual(task["status"], "done")
        exported = self.ok("/api/tasks/export")
        self.assertNotIn(lease, json.dumps(exported))
        self.assertNotIn("lease_hash", json.dumps(exported))

    def test_a_board_with_principles_refuses_a_claim_without_their_hash(self):
        principles = [{"key": "terse", "title": "Terse", "content": "Say it in one line."}]
        _, _, task, _ = self.fixture(principles=principles)
        claim = {"op": "claim", "id": task["id"], "revision": task["revision"], "actor": "worker"}
        code, raw, _ = self.call("/api/tasks/action", claim)
        self.assertEqual(code, 409, raw)
        code, _, _ = self.call("/api/tasks/action", dict(claim, principles_sha256="0" * 64))
        self.assertEqual(code, 409)
        digest = task["understanding_context"]["principles_sha256"]
        self.assertEqual(self.ok("/api/tasks/action", dict(claim, principles_sha256=digest))["task"]["status"], "doing")

    def test_upload_download_revision_and_byte_integrity(self):
        _, _, task, _ = self.fixture()
        body = b"A conversation excerpt with source context.\n"
        headers = {"X-Task-Revision": str(task["revision"]), "X-Task-Actor": "tester",
                   "X-Task-File-Name": quote("résumé-notes.txt"), "X-Task-File-Kind": "document"}
        code, raw, _ = self.call("/api/tasks/attach?id=" + task["id"], headers=headers, raw=body)
        self.assertEqual(code, 200, raw)
        task = json.loads(raw)["task"]
        attachment = task["attachments"][0]
        self.assertEqual(attachment["filename"], "résumé-notes.txt")
        code, raw, response_headers = self.call(attachment["href"])
        self.assertEqual((code, raw), (200, body))
        self.assertIn("attachment;", response_headers["Content-Disposition"])
        _, _, other, _ = self.fixture()
        code, _, _ = self.call("/api/tasks/attachment?task=" + other["id"] + "&attachment=" + attachment["id"])
        self.assertEqual(code, 404)
        code, _, _ = self.call("/api/tasks/attach?id=" + task["id"], headers=headers, raw=b"stale upload")
        self.assertEqual(code, 409)
        retained = list((self.root / ".agilevow/assets" / task["id"]).iterdir())
        self.assertEqual(len(retained), 1)
        retained[0].write_bytes(b"tampered")
        code, _, _ = self.call(attachment["href"])
        self.assertEqual(code, 409)

    def test_a_rejected_upload_leaves_no_folder_behind(self):
        _, _, task, _ = self.fixture()
        headers = {"X-Task-Revision": "999", "X-Task-Actor": "tester", "X-Task-File-Name": "late.txt", "X-Task-File-Kind": "document"}
        code, _, _ = self.call("/api/tasks/attach?id=" + task["id"], headers=headers, raw=b"stale")
        self.assertEqual(code, 409)
        self.assertFalse((self.root / ".agilevow/assets" / task["id"]).exists())

    def test_notes_cross_board_tasks_files_and_project_link_kinds(self):
        _, _, task, _ = self.fixture("area-one")
        _, _, related, _ = self.fixture("area-two")
        task = self.ok("/api/tasks/update", {"id": task["id"], "revision": task["revision"], "actor": "tester",
                       "links": [{"kind": "pr", "ref": "acme/widgets#12"}, {"kind": "task", "ref": related["id"]},
                                 {"kind": "file", "ref": "docs/brief.md"}]})["task"]
        self.assertEqual(task["status"], "ready")  # A linked pull request never finishes a task.
        self.assertNotIn("href", task["links"][0])  # an opaque reference is shown, not followed
        self.assertIn(related["id"], task["links"][1]["href"])
        code, raw, _ = self.call(task["links"][2]["href"])
        self.assertEqual(code, 200, raw)
        code, _, _ = self.call("/api/tasks/update", {"id": task["id"], "revision": task["revision"], "actor": "tester",
                               "links": [{"kind": "generation", "ref": "not-a-registered-kind"}]})
        self.assertEqual(code, 400)
        self.assertIn("pr", self.ok("/api/tasks")["link_kinds"])
        task = self.ok("/api/tasks/comment", {"id": task["id"], "revision": task["revision"], "actor": "tester",
                       "kind": "conversation", "body": "User: retain the actual source.", "source_ref": "Current conversation excerpt"})["task"]
        self.assertEqual(task["comments"][0]["author"], "tester")
        code, _, _ = self.call("/api/tasks/artifact?task=" + task["id"] + "&path=../../etc/passwd")
        self.assertIn(code, (400, 404))

    def test_local_boundary_and_what_is_served(self):
        for headers in ({"Origin": "https://outside.example"}, {"Origin": "null"}, {"Host": "outside.example"}):
            code, _, _ = self.call("/api/boards/create", {"title": "attack"}, headers)
            self.assertEqual(code, 403)
            code, _, _ = self.call("/", headers=headers)
            self.assertEqual(code, 403)
        code, _, _ = self.call("/api/tasks/unknown")
        self.assertEqual(code, 404)
        code, _, _ = self.call("/api/generation/create", {})
        self.assertEqual(code, 404)
        for raw in (b"[]", b"{not json}", b'{"value":NaN}'):
            code, _, _ = self.call("/api/tasks/create", raw=raw)
            self.assertEqual(code, 400)
        # Five files and nothing else: no directory walk, no sibling files, no old names.
        for path in ("/board.py", "/../agilevow/board.py", "/web/index.html", "/index.html", "/task-board.html", "/agilevow.sqlite3", "/.agilevow/agilevow.sqlite3"):
            code, _, _ = self.call(path)
            self.assertEqual(code, 404, path)

    def test_the_page_is_served_with_framing_protection(self):
        code, raw, headers = self.call("/")
        self.assertEqual(code, 200, raw[:100])
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn(b"<style", raw)  # the strict CSP forbids inline style, so the page must not need it
        for path in ("/board.mjs", "/i18n.mjs", "/icons.mjs", "/board.css"):
            code, body, _ = self.call(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(body, path)

    def test_an_unexpected_failure_is_logged_not_echoed(self):
        secret = "/srv/hidden/volume/that/must/not/leak"
        with patch.object(self.server.task_board, "snapshot", side_effect=FileNotFoundError(secret)), \
                patch("agilevow.server.traceback.print_exc") as logged:
            code, raw, _ = self.call("/api/tasks")
        self.assertEqual(code, 500)
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(b"hidden", raw)
        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
