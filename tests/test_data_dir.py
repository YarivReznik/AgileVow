"""Where AgileVow keeps its data, what it refuses to touch, and the project link kinds."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

from agilevow import board as board_module
from agilevow.board import TaskBoard, TaskError


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class DataDirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def task(self, store):
        board = store.create_board({"actor": "u", "source_key": "b", "title": "B"})["board"]
        plan = store.create_plan({"actor": "u", "board_id": board["id"], "source_key": "p", "title": "P"})["plan"]
        return store.create_task({"actor": "u", "plan_id": plan["id"], "key": "t", "title": "T",
                                  "outcome": "O", "acceptance": ["A"]})["task"]

    def stage(self, store, relative):
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        return {"id": "a1", "filename": "a.txt", "kind": "document", "mime": "text/plain",
                "sha256": hashlib.sha256(b"hello").hexdigest(), "size": 5, "path": relative}

    def attach(self, store, task, attachment):
        return store.attach({"id": task["id"], "revision": task["revision"], "actor": "u", "attachment": attachment})

    def test_default_layout_is_private_and_ignores_itself(self):
        store = TaskBoard(self.root)
        data = store.root / ".agilevow"
        self.assertEqual(store.db_path, data / "agilevow.sqlite3")
        self.assertEqual(mode(data), 0o700)
        self.assertEqual(mode(store.db_path), 0o600)
        self.assertEqual((data / ".gitignore").read_text(), "*\n")  # `git add .` cannot publish the database

    def test_a_directory_that_already_exists_is_left_alone(self):
        existing = self.root / "state"
        existing.mkdir()
        os.chmod(existing, 0o755)
        TaskBoard(self.root, data_dir="state")
        self.assertEqual(mode(existing), 0o755)
        self.assertFalse((existing / ".gitignore").exists())

    def test_the_database_and_its_attachments_cannot_be_split(self):
        store = TaskBoard(self.root, data_dir="state/agilevow")
        self.assertEqual(store.db_path, store.root / "state/agilevow/agilevow.sqlite3")
        self.assertEqual(store.assets_prefix, "state/agilevow/assets/")
        task = self.task(store)
        with self.assertRaises(TaskError) as refused:  # staged where the DEFAULT layout would keep it
            self.attach(store, task, self.stage(store, ".agilevow/assets/a1"))
        self.assertEqual(refused.exception.status, 400)
        self.assertIn("state/agilevow/assets/", str(refused.exception))
        stored = self.attach(store, task, self.stage(store, "state/agilevow/assets/a1"))["task"]
        self.assertEqual(stored["attachments"][0]["path"], "state/agilevow/assets/a1")

    def test_the_data_dir_must_stay_inside_the_project(self):
        for bad in ("/tmp/elsewhere", "../outside", "a/../b", "", "a//b", "a\\b"):
            with self.assertRaises(TaskError, msg=repr(bad)):
                TaskBoard(self.root, data_dir=bad)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_mistyped_project_folder_is_refused_not_created(self):
        with self.assertRaises(TaskError) as refused:
            TaskBoard(self.root / "typo" / "deeper")
        self.assertIn("does not exist", str(refused.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reopening_finds_the_same_data(self):
        first = TaskBoard(self.root)
        task = self.task(first)
        again = TaskBoard(self.root)
        self.assertEqual(again.get_task(task["id"])["task"]["id"], task["id"])
        self.assertEqual(mode(again.db_path), 0o600)

    def test_project_link_kinds(self):
        store = TaskBoard(self.root, link_kinds=("pr", "issue"))
        self.assertEqual(sorted(store.link_kinds), ["file", "issue", "pr", "task", "url"])
        self.assertEqual(sorted(store.snapshot()["link_kinds"]), ["file", "issue", "pr", "task", "url"])
        task = self.task(store)
        links = [{"kind": "pr", "ref": "acme/widgets#12"}, {"kind": "issue", "ref": "acme/widgets#7"}]
        updated = store.update_task({"id": task["id"], "revision": task["revision"], "actor": "u", "links": links})["task"]
        self.assertEqual([item["ref"] for item in updated["links"]], ["acme/widgets#12", "acme/widgets#7"])
        with self.assertRaises(TaskError):  # not registered on this board's server
            store.update_task({"id": task["id"], "revision": updated["revision"], "actor": "u",
                               "links": [{"kind": "commit", "ref": "abc123"}]})
        for bad in ("File", "pr!", "x" * 21, "url", "", "9pr", 5):
            with self.assertRaises(TaskError, msg=repr(bad)):
                TaskBoard(self.root, link_kinds=(bad,))

    def test_an_unsupported_platform_fails_with_a_clear_message(self):
        with patch.object(board_module, "os", types.SimpleNamespace()):
            with self.assertRaisesRegex(RuntimeError, "POSIX"):
                TaskBoard(self.root)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
