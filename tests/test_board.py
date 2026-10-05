"""Task-board durability, ownership, document and acceptance boundaries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agilevow.board import TaskBoard, TaskError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TaskBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.store = TaskBoard(self.root, clock=self.clock)
        self.board = self.store.create_board({"actor": "planner", "source_key": "project", "title": "Project", "scope": "shared"})["board"]
        self.plan = self.store.create_plan({"actor": "planner", "board_id": self.board["id"], "source_key": "plan", "title": "Plan", "kind": "code"})["plan"]

    def task(self, **fields):
        request = {"actor": "planner", "plan_id": self.plan["id"], "key": "first", "title": "First task",
                   "outcome": "The intended result is visible", "acceptance": ["Observed in the running app"], "status": "ready"}
        request.update(fields)
        return self.store.create_task(request)["task"]

    def action(self, task, op, **fields):
        return self.store.action(dict({"id": task["id"], "revision": task["revision"], "actor": "worker", "op": op}, **fields))

    def update(self, task, **fields):
        return self.store.update_task(dict({"id": task["id"], "revision": task["revision"], "actor": "worker"}, **fields))["task"]

    def done(self, task):
        claim = self.action(task, "claim")
        task = self.update(claim["task"], lease=claim["lease"], evidence=[{"criterion": i, "ref": f"capture:{i}", "note": "Observed"} for i in range(len(task["acceptance"]))], checklist_checks=[{"criterion":i,"checked":True} for i in range(len(task["acceptance"]))])
        task = self.action(task, "transition", status="review", lease=claim["lease"])["task"]
        return self.validate_done(task)

    def validate_done(self, task):
        task = self.action(task, "transition", status="post_validation")["task"]
        return self.action(task, "validate", validation={"result":"pass","summary":"Inspected all outputs", "principles":[],
            "checks":[{"criterion":i,"passed":True,"observed":"Matches the intended result","ref":"capture"} for i in range(len(task["acceptance"]))]})["task"]

    def assert_error(self, expected_status, call, *args, **kwargs):
        with self.assertRaises(TaskError) as caught:
            call(*args, **kwargs)
        self.assertEqual(expected_status, caught.exception.status, str(caught.exception))

    def import_request(self, **fields):
        request = {"actor": "planner", "board_id": self.board["id"], "source_key": "import", "title": "Imported plan", "kind": "mixed",
                   "tasks": [{"key": "a", "title": "A", "outcome": "A outcome", "acceptance": ["A observed"], "status": "ready"},
                             {"key": "b", "title": "B", "outcome": "B outcome", "acceptance": ["B observed"], "depends_on": ["a"]}]}
        request.update(fields)
        return request

    def test_restart_durability_and_secret_free_reads(self):
        task = self.task()
        claim = self.action(task, "claim")
        restarted = TaskBoard(self.root, clock=self.clock)
        public = restarted.get_task(task["id"])
        self.assertEqual(claim["task"], public["task"])
        self.assertEqual(["create_task", "claim"], [event["op"] for event in public["events"]])
        archive = restarted.export()
        serialized = json.dumps([public, archive, restarted.snapshot()])
        self.assertNotIn(claim["lease"], serialized)
        self.assertNotIn("lease_hash", serialized)
        self.assertTrue(archive["archival"])
        with closing(sqlite3.connect(self.store.db_path)) as db:
            stored = db.execute("SELECT lease_hash FROM task_items WHERE id=?", (task["id"],)).fetchone()[0]
        self.assertNotEqual(stored, claim["lease"])
        renewed = restarted.action({"id": task["id"], "revision": claim["task"]["revision"], "actor": "worker", "op": "renew", "lease": claim["lease"]})
        self.assertEqual(3, renewed["task"]["revision"])

    def test_atomic_claim_between_independent_stores(self):
        task = self.task()
        stores = [TaskBoard(self.root, clock=self.clock), TaskBoard(self.root, clock=self.clock)]
        barrier = threading.Barrier(2)

        def claim(index):
            barrier.wait()
            try:
                return stores[index].action({"id": task["id"], "revision": task["revision"], "actor": f"worker-{index}", "op": "claim"})
            except TaskError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(1, sum(isinstance(value, dict) for value in results))
        self.assertIn(409, results)
        self.assertEqual(2, self.store.get_task(task["id"])["task"]["revision"])

    def test_stale_revision_rolls_back_without_history(self):
        task = self.task()
        edited = self.update(task, title="Changed title")
        before = self.store.export()
        self.assert_error(409, self.update, task, title="Lost update")
        self.assertEqual(before, self.store.export())
        self.assertEqual("Changed title", edited["title"])

    def test_expiry_requires_explicit_recover_and_old_token_stays_invalid(self):
        claim = self.action(self.task(), "claim", lease_seconds=60)
        self.clock.now += 60
        expired = self.store.get_task(claim["task"]["id"])["task"]
        self.assertEqual("doing", expired["status"])
        self.assertEqual(claim["task"]["revision"], expired["revision"])
        self.assert_error(409, self.action, expired, "renew", lease=claim["lease"])
        recovered = self.action(expired, "recover", note="Worker session ended")["task"]
        reclaimed = self.action(recovered, "claim")
        self.assertNotEqual(claim["lease"], reclaimed["lease"])
        self.assert_error(409, self.update, reclaimed["task"], lease=claim["lease"], next_step="Late write")
        self.assert_error(409, self.action, reclaimed["task"], "recover", note="Still alive")

    def test_only_owner_can_write_or_release_live_claim(self):
        claim = self.action(self.task(), "claim")
        task = claim["task"]
        self.assert_error(400, self.update, task, next_step="No token")
        self.assert_error(409, self.action, task, "release", actor="stranger", lease=claim["lease"], note="Handoff")
        self.assert_error(409, self.update, task, owner="another", lease=claim["lease"])
        released = self.action(task, "release", lease=claim["lease"], note="Next: compare captures")["task"]
        self.assertEqual("ready", released["status"])
        self.assertEqual("Next: compare captures", released["next_step"])
        self.assertIsNone(released["claim_expires_at"])

    def test_explicit_block_and_correction_revoke_claim_without_owner_token(self):
        claim = self.action(self.task(), "claim")
        self.assert_error(400, self.action, claim["task"], "transition", actor="planner", status="blocked")
        blocked = self.action(claim["task"], "transition", actor="planner", status="blocked", note="New source decision needed")["task"]
        self.assertEqual("New source decision needed", blocked["blocked_reason"])
        ready = self.action(blocked, "transition", status="ready")["task"]
        claim2 = self.action(ready, "claim")
        corrected = self.update(claim2["task"], actor="planner", acceptance=["Revised observed criterion"], note="User corrected the scope")
        self.assertEqual("backlog", corrected["status"])
        self.assertIsNone(corrected["claim_expires_at"])
        ready2 = self.action(corrected, "transition", status="ready")["task"]
        claim3 = self.action(ready2, "claim")
        self.assert_error(409, self.update, claim3["task"], lease=claim2["lease"], next_step="Stale")

    def test_evidence_and_freshness_gate_done_with_explicit_reconciliation(self):
        source = self.root / "brief.md"
        source.write_text("Version one")
        task = self.task(sources=["brief.md"], acceptance=["Input works", "Capture compared"])
        claim = self.action(task, "claim")
        review = self.action(claim["task"], "transition", status="review", lease=claim["lease"])["task"]
        self.assert_error(409, self.action, review, "transition", status="post_validation")
        review = self.update(review, evidence=[{"criterion": 0, "ref": "input-log","note":"Inputs checked"}, {"criterion": 1, "ref": "capture-log","note":"Compared"}], checklist_checks=[{"criterion":i,"checked":True} for i in range(2)])
        source.write_text("Version two")
        drifted = self.store.get_task(task["id"])["task"]
        self.assertTrue(drifted["source_changed"])
        self.assertEqual("brief.md", drifted["changed_sources"][0]["path"])
        self.assertNotEqual(drifted["changed_sources"][0]["recorded_sha256"], drifted["changed_sources"][0]["current_sha256"])
        self.assert_error(409, self.action, review, "transition", status="post_validation")
        reconciled = self.action(review, "reconcile", note="Read revision two; criteria still apply")["task"]
        self.assertFalse(reconciled["source_changed"])
        self.assertEqual("backlog", reconciled["status"])
        self.assertEqual([], reconciled["evidence"])
        ready = self.action(reconciled, "transition", status="ready")["task"]
        done = self.done(ready)
        self.assertEqual("done", done["status"])
        reopened = self.update(done, outcome="A revised result", note="New requirement")
        self.assertEqual("backlog", reopened["status"])
        self.assertEqual([], reopened["evidence"])
        self.assertEqual("Version two", source.read_text())

    def test_source_drift_blocks_claim_and_missing_source_is_visible(self):
        source = self.root / "brief.md"
        source.write_text("One")
        task = self.task(sources=["brief.md"])
        source.unlink()
        current = self.store.get_task(task["id"])["task"]
        self.assertTrue(current["changed_sources"][0]["source_missing"])
        self.assert_error(409, self.action, current, "claim")
        before = self.store.export()
        self.assert_error(400, self.action, current, "reconcile", note="Missing file")
        self.assertEqual(before, self.store.export())

    def test_plan_source_correction_reopens_all_affected_tasks(self):
        (self.root / "brief.md").write_text("One")
        self.plan = self.store.create_plan({"board_id": self.board["id"], "source_key": "sourced", "title": "Source plan", "sources": ["brief.md"], "actor": "planner"})["plan"]
        done = self.done(self.task(key="done"))
        claimed = self.action(self.task(key="doing"), "claim")
        (self.root / "brief.md").write_text("Two")
        self.store.update_plan({"id": self.plan["id"], "revision": self.plan["revision"], "actor": "planner", "sources": ["brief.md"], "note": "Read latest direction"})
        for original in (done, claimed["task"]):
            task = self.store.get_task(original["id"])["task"]
            self.assertEqual("backlog", task["status"])
            self.assertFalse(task["source_changed"])
            self.assertEqual([], task["evidence"])
            self.assertIsNone(task["claim_expires_at"])
            self.assertGreater(task["revision"], original["revision"])

    def test_dependency_cycles_cross_plan_and_incomplete_dependencies_rejected(self):
        first = self.task(status="backlog")
        second = self.task(key="second", status="backlog", depends_on=[first["id"]])
        self.assert_error(400, self.update, first, depends_on=[second["id"]], note="Would cycle")
        self.assert_error(409, self.action, second, "transition", status="ready")
        self.assert_error(404, self.update, second, depends_on=["unknown"], note="Unknown")
        other_plan = self.store.create_plan({"actor": "planner", "board_id": self.board["id"], "source_key": "other", "title": "Other"})["plan"]
        self.assert_error(400, self.task, key="cross", plan_id=other_plan["id"], status="backlog", depends_on=[first["id"]])
        ready = self.action(first, "transition", status="ready")["task"]
        self.done(ready)
        self.assertEqual("ready", self.action(second, "transition", status="ready")["task"]["status"])

    def test_import_atomic_idempotent_and_different_payload_conflicts(self):
        request = self.import_request()
        first = self.store.import_plan(request)
        again = self.store.import_plan(dict(request, actor="second planner"))
        self.assertTrue(first["imported"])
        self.assertFalse(again["imported"])
        self.assertEqual(first["plan"], again["plan"])
        self.assertEqual(first["tasks"], again["tasks"])
        self.assertEqual(first["tasks"][0]["id"], first["tasks"][1]["depends_on"][0])
        before = self.store.export()
        self.assert_error(409, self.store.import_plan, dict(request, title="Conflicting title"))
        self.assertEqual(before, self.store.export())
        for malformed in ("unknown", "cycle", "ready", "duplicate"):
            req = self.import_request(source_key=malformed)
            if malformed == "unknown":
                req["tasks"][1]["depends_on"] = ["missing"]
            elif malformed == "cycle":
                req["tasks"][0]["depends_on"] = ["b"]
                req["tasks"][0]["status"] = "backlog"
            elif malformed == "ready":
                req["tasks"][1]["status"] = "ready"
            else:
                req["tasks"][1]["key"] = "a"
            self.assert_error(409 if malformed == "ready" else 400, self.store.import_plan, req)
            self.assertEqual(before, self.store.export())

    def test_multiple_boards_scopes_kinds_and_related_links(self):
        other = self.store.create_board({"actor": "planner", "source_key": "docs", "title": "Another board", "scope": "documentation"})["board"]
        request = self.import_request()
        first = self.store.import_plan(request)
        second = self.store.import_plan(dict(request, board_id=other["id"], kind="audio experiment"))
        self.assertNotEqual(first["plan"]["id"], second["plan"]["id"])
        target = second["tasks"][0]
        linked = self.update(first["tasks"][0], links=[{"kind": "task", "ref": target["id"], "label": "Related exploration"}], tags=["prototype", "audio"])
        self.assertEqual(target["id"], linked["links"][0]["ref"])
        filtered = self.store.snapshot(board_id=other["id"])
        self.assertEqual([other], filtered["boards"])
        self.assertTrue(all(item["board_id"] == other["id"] for item in filtered["plans"] + filtered["tasks"]))
        self.assert_error(400, self.store.snapshot, plan_id=first["plan"]["id"], board_id=other["id"])
        self.assert_error(404, self.update, linked, links=[{"kind": "task", "ref": "unknown"}])

    def test_board_creation_replay_and_revisions(self):
        req = {"actor": "planner", "source_key": "project", "title": "Project", "scope": "shared"}
        same = self.store.create_board(req)
        self.assertFalse(same["created"])
        self.assertEqual(self.board, same["board"])
        self.assert_error(409, self.store.create_board, dict(req, title="Different"))
        changed = self.store.update_board({"id": self.board["id"], "revision": 1, "actor": "planner", "description": "Shared tools"})["board"]
        self.assertEqual(2, changed["revision"])
        self.assert_error(409, self.store.update_board, {"id": self.board["id"], "revision": 1, "actor": "planner", "title": "Stale"})

    def test_safe_source_paths_and_expected_hash(self):
        (self.root / "brief.md").write_text("One")
        (self.root / "alias.md").symlink_to(self.root / "brief.md")
        (self.root / "alias-dir").symlink_to(self.root, target_is_directory=True)
        for path in ("../brief.md", "/etc/passwd", "brief.md/..", "./brief.md", "alias.md", "alias-dir/brief.md"):
            self.assert_error(400, self.task, sources=[path])
        self.assert_error(409, self.task, sources=[{"path": "brief.md", "sha256": "0" * 64}])
        self.assertEqual([], self.store.snapshot()["tasks"])

    def test_comments_and_uploads_are_append_only_bound_to_task_revision(self):
        task = self.task()
        comment_req = {"id": task["id"], "revision": task["revision"], "actor": "planner", "kind": "conversation", "body": "User: compare the two captures.", "source_ref": "User-pasted planning conversation"}
        task = self.store.comment(comment_req)["task"]
        self.assertEqual("conversation", task["comments"][0]["kind"])
        self.assertEqual("planner", task["comments"][0]["author"])
        self.assert_error(409, self.store.comment, comment_req)
        self.assert_error(400, self.update, task, comments=[])
        upload = self.root / ".agilevow/assets/file-one.txt"
        upload.parent.mkdir(parents=True)
        upload.write_bytes(b"retained document")
        attachment = {"id": "file-one", "filename": "brief.txt", "kind": "document", "sha256": hashlib.sha256(upload.read_bytes()).hexdigest(), "size": upload.stat().st_size, "mime": "text/plain", "path": ".agilevow/assets/file-one.txt"}
        task = self.store.attach({"id": task["id"], "revision": task["revision"], "actor": "planner", "attachment": attachment})["task"]
        self.assertEqual(1, len(task["attachments"]))
        self.assertEqual(attachment["sha256"], task["attachments"][0]["sha256"])
        self.assert_error(400, self.update, task, attachments=[])
        self.assert_error(409, self.store.attach, {"id": task["id"], "revision": task["revision"], "actor": "planner", "attachment": attachment})
        claim = self.action(task, "claim")
        self.assert_error(400, self.store.comment, dict(comment_req, revision=claim["task"]["revision"]))
        done_comment = self.store.comment(dict(comment_req, revision=claim["task"]["revision"], actor="worker", lease=claim["lease"]))
        self.assertEqual(2, len(done_comment["task"]["comments"]))
        self.assertEqual(1, len(TaskBoard(self.root).get_task(task["id"])["task"]["attachments"]))

    def test_malformed_inputs_are_safe_and_transactional(self):
        before = self.store.export()
        for req in (None, [], {}, {"actor": "planner", "title": ["bad"]}, {"actor": "planner", "unexpected": 1}, {"actor": "planner", "title": float("nan")}):
            self.assert_error(400, self.store.create_plan, req)
        for fields in ({"acceptance": []}, {"acceptance": [None]}, {"priority": True}, {"status": "doing"}, {"title": ""}, {"sources": {}}, {"links": [{"kind": "url", "ref": "javascript:alert(1)"}]}, {"evidence": [{"criterion": 8, "ref": "fake"}]}):
            self.assert_error(400, self.task, **fields)
        self.assertEqual(before, self.store.export())

    def test_no_bypass_to_done_or_automatic_completion_from_a_linked_run(self):
        task = self.task(links=[{"kind": "url", "ref": "https://ci.example.com/runs/1", "label": "Green CI run"}])
        self.assertEqual("ready", task["status"])
        self.assert_error(409, self.action, task, "transition", status="doing")
        self.assert_error(409, self.action, task, "transition", status="done")
        task = self.done(task)
        reopened = self.action(task, "transition", status="backlog", note="Another pass requested")["task"]
        self.assertEqual([], reopened["evidence"])
        cancelled = self.action(reopened, "transition", status="cancelled", note="No longer in this scope")["task"]
        self.assertEqual("backlog", self.action(cancelled, "transition", status="backlog", note="Scope returned")["task"]["status"])

    def test_expired_worker_cannot_submit_review_or_evidence(self):
        claim = self.action(self.task(), "claim", lease_seconds=60)
        self.clock.now += 61
        before = self.store.export()
        self.assert_error(409, self.action, claim["task"], "transition", status="review", lease=claim["lease"])
        self.assert_error(409, self.update, claim["task"], evidence=[{"criterion": 0, "ref": "late"}], lease=claim["lease"])
        self.assertEqual(before, self.store.export())

    def test_reopened_dependency_prevents_dependent_completion(self):
        prerequisite = self.done(self.task())
        dependent = self.task(key="dependent", depends_on=[prerequisite["id"]])
        claim = self.action(dependent, "claim")
        dependent = self.update(claim["task"], lease=claim["lease"], evidence=[{"criterion": 0, "ref": "capture", "note":"Observed"}], checklist_checks=[{"criterion":0,"checked":True}])
        dependent = self.action(dependent, "transition", status="review", lease=claim["lease"])["task"]
        prerequisite = self.update(prerequisite, acceptance=["Updated requirement"], note="Re-check the foundation")
        self.assertEqual("backlog", prerequisite["status"])
        self.assert_error(409, self.validate_done, dependent)
        prerequisite = self.action(prerequisite, "transition", status="ready")["task"]
        self.done(prerequisite)
        dependent=self.store.get_task(dependent["id"])["task"]
        if dependent["status"] == "post_validation":
            result=self.action(dependent,"validate",validation={"result":"pass","summary":"Rechecked after dependency","principles":[],"checks":[{"criterion":0,"passed":True,"observed":"Dependency is complete","ref":"capture"}]})["task"]
        else:
            result=self.validate_done(dependent)
        self.assertEqual("done",result["status"])

    def test_attachments_accept_over_source_limit_and_reject_corruption(self):
        task = self.task()
        upload = self.root / ".agilevow/assets/large.bin"
        upload.parent.mkdir(parents=True)
        # Sparse regular file: exercise hashing above the watched-document limit.
        with upload.open("wb") as stream:
            stream.truncate(17 * 1024 * 1024)
        attachment = {"id": "large", "filename": "large.bin", "kind": "asset", "sha256": hashlib.sha256(upload.read_bytes()).hexdigest(), "size": upload.stat().st_size, "mime": "application/octet-stream", "path": ".agilevow/assets/large.bin"}
        self.assert_error(400, self.task, key="source-too-large", sources=[attachment["path"]])
        req = {"id": task["id"], "revision": task["revision"], "actor": "planner", "attachment": attachment}
        self.assert_error(409, self.store.attach, dict(req, attachment=dict(attachment, sha256="0" * 64)))
        retained = self.store.attach(req)["task"]
        self.assertEqual(17 * 1024 * 1024, retained["attachments"][0]["size"])
        outside = dict(attachment, path="brief.bin")
        self.assert_error(400, self.store.attach, dict(req, revision=retained["revision"], attachment=outside))
        self.assert_error(400, self.store.attach, dict(req, revision=retained["revision"], attachment=dict(attachment, size=33 * 1024 * 1024)))

    def test_reconciled_inherited_fingerprint_survives_task_source_edit(self):
        (self.root / "brief.md").write_text("One")
        (self.root / "extra.md").write_text("Extra")
        self.plan = self.store.create_plan({"board_id": self.board["id"], "source_key": "inherited", "title": "Inherited sources", "sources": ["brief.md"], "actor": "planner"})["plan"]
        task = self.task()
        (self.root / "brief.md").write_text("Two")
        reconciled = self.action(task, "reconcile", note="Read version two")["task"]
        self.assertFalse(reconciled["source_changed"])
        updated = self.update(reconciled, sources=["extra.md"], note="Add a supporting source")
        self.assertFalse(updated["source_changed"])
        self.assertEqual({"brief.md", "extra.md"}, {source["path"] for source in updated["sources"]})
        self.assertEqual(reconciled["sources"][0], next(source for source in updated["sources"] if source["path"] == "brief.md"))

    def test_revoked_supplied_lease_rejected_outside_doing_for_every_write(self):
        claim = self.action(self.task(), "claim", lease_seconds=60)
        self.clock.now += 61
        recovered = self.action(claim["task"], "recover", note="Abandoned session")["task"]
        base = {"id": recovered["id"], "revision": recovered["revision"], "actor": "worker", "lease": claim["lease"]}
        upload = self.root / ".agilevow/assets/revoked.txt"
        upload.parent.mkdir(parents=True)
        upload.write_bytes(b"retained")
        attachment = {"id": "revoked", "filename": "retained.txt", "kind": "document", "sha256": hashlib.sha256(upload.read_bytes()).hexdigest(), "size": upload.stat().st_size, "mime": "text/plain", "path": ".agilevow/assets/revoked.txt"}
        before = self.store.export()
        self.assert_error(409, self.store.update_task, dict(base, next_step="Late worker write"))
        self.assert_error(409, self.store.comment, dict(base, body="Late worker note"))
        self.assert_error(409, self.store.attach, dict(base, attachment=attachment))
        self.assert_error(409, self.store.action, dict(base, op="transition", status="blocked", note="Late worker block"))
        self.assert_error(409, self.store.action, dict(base, op="claim"))
        self.assertEqual(before, self.store.export())
        # A deliberately manual edit remains possible; an HTTP null lease is absent.
        manual = self.store.update_task(dict(base, lease=None, next_step="Planner handoff"))["task"]
        self.assertEqual("Planner handoff", manual["next_step"])


if __name__ == "__main__":
    unittest.main()
