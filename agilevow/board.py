"""Durable, revision-checked boards, plans and tasks (no worker execution).

SQLite transactions serialize edits across server instances. Lease secrets are
hashed at rest and returned only from claim. Export is an audit archive, not a
restore format; the latest HISTORY_LIMIT events for each entity are retained.

POSIX only: source and attachment paths are walked with O_NOFOLLOW from a
directory descriptor, which Windows does not offer.
"""

from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from urllib.parse import urlsplit


HISTORY_LIMIT = 500
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
STATUSES = {"backlog", "ready", "doing", "review", "post_validation", "done", "blocked", "cancelled"}
# A project can add kinds of its own (`pr`, `issue`, `commit`...) with TaskBoard(link_kinds=...).
CORE_LINK_KINDS = frozenset({"file", "url", "task"})
DATA_DIR = ".agilevow"          # relative to the project root; holds the database and attachments
ASSETS_DIR = "assets"
TRANSITIONS = {
    "backlog": {"ready", "blocked", "cancelled"},
    "ready": {"backlog", "blocked", "cancelled"},
    "doing": {"review", "post_validation", "blocked", "cancelled"},
    "review": {"post_validation", "ready", "blocked", "cancelled"},
    "post_validation": {"blocked", "cancelled"},
    "done": {"backlog"},
    "blocked": {"ready", "backlog", "cancelled"},
    "cancelled": {"backlog"},
}
BOARD_FIELDS = {"source_key", "title", "description", "scope", "principles"}
PLAN_FIELDS = (BOARD_FIELDS - {"principles"}) | {"board_id", "kind", "sources"}
TASK_FIELDS = {"key", "title", "outcome", "acceptance", "depends_on", "priority",
               "status", "owner", "next_step", "blocked_reason", "links", "evidence", "sources", "tags"}
EDIT_FIELDS = (TASK_FIELDS - {"key", "status", "blocked_reason"}) | {"checklist_checks", "correction_checks"}
CORRECTION_FIELDS = {"outcome", "acceptance", "depends_on", "sources"}


class TaskError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _text(value, name, limit=4000, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise TaskError(f"{name} must be text of at most {limit} characters")
    value = value.strip()
    if required and not value:
        raise TaskError(f"{name} is required")
    return value


def _list(value, name, limit=100):
    if not isinstance(value, list) or len(value) > limit:
        raise TaskError(f"{name} must be an array of at most {limit} items")
    return value


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise TaskError(f"{name} must be an integer from {low} to {high}")
    return value


def _request(req, allowed):
    if not isinstance(req, dict):
        raise TaskError("request must be an object")
    try:
        encoded = json.dumps(req, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_BODY_BYTES:
            raise TaskError("request is too large")
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskError("request must contain valid JSON values") from exc
    if not all(isinstance(key, str) for key in req):
        raise TaskError("request keys must be strings")
    unknown = set(req) - allowed
    if unknown:
        raise TaskError("unknown fields: " + ", ".join(sorted(unknown)))
    return _text(req.get("actor", ""), "actor", 128, True)


def _key(value, name="key"):
    value = _text(value, name, 160, True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
        raise TaskError(f"{name} must use letters, numbers, dots, underscores, colons, slashes or hyphens")
    return value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskBoard:
    def __init__(self, root, data_dir=DATA_DIR, clock=time.time, link_kinds=()):
        if not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")):
            raise RuntimeError("agilevow needs a POSIX system (macOS or Linux); Windows is not supported")
        self.root = Path(root).resolve()
        if not self.root.is_dir():  # a typo in --root must not quietly create a new folder tree
            raise TaskError(f"the project folder does not exist: {self.root}")
        # The data directory must stay inside the project root: attachments are fingerprinted
        # through the same symlink-refusing walk as sources, and that walk starts at the root.
        self.data_dir = self._path(data_dir)
        self.assets_prefix = f"{self.data_dir}/{ASSETS_DIR}/"
        self.db_path = self.root / self.data_dir / "agilevow.sqlite3"
        extra = set()
        for kind in link_kinds:
            if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", kind) or kind in CORE_LINK_KINDS:
                raise TaskError(f"link kinds must be new lowercase names of at most 20 characters: {kind!r}")
            extra.add(kind)
        self.link_kinds = CORE_LINK_KINDS | extra
        self.clock = clock
        self._local = threading.local()
        self._create_data_dir()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS task_board_meta (version INTEGER NOT NULL);
                INSERT INTO task_board_meta(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM task_board_meta);
                CREATE TABLE IF NOT EXISTS task_boards (
                    id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
                    body TEXT NOT NULL, create_fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_plans (
                    id TEXT PRIMARY KEY, board_id TEXT NOT NULL REFERENCES task_boards(id),
                    source_key TEXT NOT NULL, body TEXT NOT NULL, import_fingerprint TEXT,
                    UNIQUE(board_id, source_key)
                );
                CREATE TABLE IF NOT EXISTS task_items (
                    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES task_plans(id),
                    task_key TEXT NOT NULL, body TEXT NOT NULL, lease_hash TEXT,
                    UNIQUE(plan_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL,
                    task_id TEXT, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_items_plan ON task_items(plan_id);
                CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, id);
            """)
            if db.execute("SELECT version FROM task_board_meta").fetchone()[0] != 1:
                raise TaskError("unsupported task board database version", 409)
            db.commit()
        os.chmod(self.db_path, 0o600)

    def _create_data_dir(self):
        """Owner-only, and self-ignoring, so `git add .` cannot publish the database."""
        directory = self.root / self.data_dir
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if created:
            os.chmod(directory, 0o700)
            (directory / ".gitignore").write_text("*\n")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def _transaction(self, write=False):
        db = self._connect()
        old_cache = getattr(self._local, "source_cache", None)
        self._local.source_cache = {}
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise TaskError("duplicate or conflicting plan/task", 409) from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            self._local.source_cache = old_cache

    def _now(self):
        value = float(self.clock())
        if not math.isfinite(value):
            raise TaskError("invalid clock")
        return value

    @staticmethod
    def _path(value):
        value = _text(value, "source path", 1024, True)
        path = PurePosixPath(value)
        if path.is_absolute() or "\\" in value or any(p in ("", ".", "..") for p in value.split("/")):
            raise TaskError("paths must be repository-relative without dot components")
        return value

    def _hash_source(self, value, max_bytes=MAX_SOURCE_BYTES):
        path = self._path(value)
        cache = getattr(self._local, "source_cache", None)
        cache_key = (path, max_bytes)
        if cache is not None and cache_key in cache:
            return dict(cache[cache_key])
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        file_fd = None
        try:
            parts = path.split("/")
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise TaskError(f"file must be regular and at most {max_bytes} bytes")
            digest = hashlib.sha256()
            total = 0
            while True:
                block = os.read(file_fd, 65536)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise TaskError("file exceeds size limit")
                digest.update(block)
            result = {"path": path, "sha256": digest.hexdigest()}
            if cache is not None:
                cache[cache_key] = result
            return dict(result)
        except OSError as exc:
            raise TaskError(f"source is missing, unreadable or uses a symlink: {path}") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory)

    def _sources(self, values):
        result = []
        seen = set()
        for value in _list(values, "sources"):
            expected = None
            if isinstance(value, dict):
                if set(value) - {"path", "sha256"}:
                    raise TaskError("source entries support path and sha256 only")
                expected = value.get("sha256")
                value = value.get("path")
                if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                    raise TaskError("source sha256 must be 64 lowercase hexadecimal characters")
            source = self._hash_source(value)
            if expected is not None and expected != source["sha256"]:
                raise TaskError(f"source fingerprint changed: {source['path']}", 409)
            if source["path"] in seen:
                raise TaskError("duplicate source path")
            seen.add(source["path"])
            result.append(source)
        return sorted(result, key=lambda item: item["path"])

    @staticmethod
    def _merge_sources(inherited, own):
        result = {item["path"]: item for item in own}
        # A plan's retained fingerprints cannot be silently replaced by a task.
        result.update({item["path"]: item for item in inherited})
        return sorted(result.values(), key=lambda item: item["path"])

    def _changed_sources(self, sources):
        changed = []
        for source in sources:
            current = None
            try:
                current = self._hash_source(source["path"])["sha256"]
            except TaskError:
                pass
            if current != source["sha256"]:
                changed.append({"path": source["path"], "recorded_sha256": source["sha256"],
                                "current_sha256": current, "source_missing": current is None})
        return changed

    def _changed(self, sources):
        return bool(self._changed_sources(sources))

    @staticmethod
    def _principles(values):
        result, seen = [], set()
        for item in _list(values, "principles", 24):
            if not isinstance(item, dict) or set(item) != {"key", "title", "content"}:
                raise TaskError("principles require key, title and full content")
            key = _key(item["key"], "principle key")
            if key in seen:
                raise TaskError("duplicate principle key")
            seen.add(key)
            result.append({"key": key, "title": _text(item["title"], "principle title", 300, True),
                           "content": _text(item["content"], "principle content", 8000, True)})
        if sum(len(x["title"]) + len(x["content"]) for x in result) > 64000:
            raise TaskError("combined principles exceed 64000 characters")
        return result

    def _principles_context(self, db, task):
        board = self._board(db, task["board_id"])
        principles = board.get("principles", [])
        digest = hashlib.sha256(_dump(principles).encode()).hexdigest()
        intro = ("Read these board-wide user/project requirements before interpreting the task. "
                 "Include their full text when handing the task to another worker. "
                 "They do not replace platform/system instructions or newer explicit user instructions.")
        text = "# Binding principles\n" + intro + "\n\n"
        text += "\n\n".join(f"{i+1}. {p['title']}\n{p['content']}" for i, p in enumerate(principles)) or "No board principles have been defined."
        text += f"\n\n# Task\n{task['title']}\n\n{task['outcome']}\n\n# Acceptance\n"
        text += "\n".join(f"- {criterion}" for criterion in task["acceptance"])
        text += "\n\n# Sources\n" + "\n".join(source["path"] for source in task["sources"])
        text += "\n\n# Next step\n" + task["next_step"]
        return {"board_id": board["id"], "board_revision": board["revision"],
                "principles_sha256": digest, "principles": principles,
                "read_order": ["principles", "outcome", "acceptance", "sources", "next_step"],
                "authority": "user/project requirements", "text": text}

    def _acknowledge_principles(self, db, task, req, actor, required=False):
        context = self._principles_context(db, task)
        supplied = req.get("principles_sha256")
        prior = task.get("principles_acknowledgement", {})
        if supplied is None and not required:
            if not context["principles"] and not prior:
                return
            if prior.get("sha256") == context["principles_sha256"] and prior.get("actor") == actor:
                return
        if supplied != context["principles_sha256"]:
            raise TaskError("read current understanding_context and supply its principles_sha256 before starting or completing work", 409)
        task["principles_acknowledgement"] = {
            "sha256": supplied, "board_revision": context["board_revision"],
            "actor": actor, "at": self._now()}

    def _public(self, db, task):
        context = self._principles_context(db, task)
        public = {"understanding_context": context, **task}
        ack = task.get("principles_acknowledgement", {})
        public["principles_changed"] = bool(ack and ack.get("sha256") != context["principles_sha256"])
        public["principles_need_acknowledgement"] = bool(context["principles"] or ack) and ack.get("sha256") != context["principles_sha256"]
        marks = {v["criterion"]: v for v in task.get("checklist_checks", [])}
        public["checklist"] = [dict(criterion=i, text=text, **marks.get(i, {"checked": False}))
                               if i not in marks else dict(marks[i], text=text)
                               for i, text in enumerate(task["acceptance"])]
        public["changed_sources"] = self._changed_sources(task["sources"])
        public["source_changed"] = bool(public["changed_sources"])
        return public

    @staticmethod
    def _board(db, board_id):
        board_id = _text(board_id, "board id", 160, True)
        row = db.execute("SELECT body FROM task_boards WHERE id=?", (board_id,)).fetchone()
        if not row:
            raise TaskError("unknown board", 404)
        return json.loads(row["body"])

    @staticmethod
    def _plan(db, plan_id):
        plan_id = _text(plan_id, "plan id", 160, True)
        row = db.execute("SELECT body FROM task_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise TaskError("unknown plan", 404)
        return json.loads(row["body"])

    @staticmethod
    def _task(db, task_id):
        task_id = _text(task_id, "task id", 160, True)
        row = db.execute("SELECT body,lease_hash FROM task_items WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise TaskError("unknown task", 404)
        return json.loads(row["body"]), row["lease_hash"]

    @staticmethod
    def _revision(item, req):
        revision = _int(req.get("revision"), "revision", 1, 2**53 - 1)
        if item["revision"] != revision:
            raise TaskError("stale revision; reload the current item", 409)

    def _event(self, db, item, actor, op, note="", changes=(), previous=None, plan=False, board=False):
        event = {"board_id": item["id"] if board else item["board_id"],
                 "plan_id": None if board else item["id"] if plan else item["plan_id"],
                 "task_id": None if plan or board else item["id"], "revision": item["revision"],
                 "at": item["updated_at"], "actor": actor, "op": op, "note": note,
                 "changes": sorted(changes), "from_status": previous,
                 "to_status": None if plan or board else item["status"]}
        if board and ("principles" in changes or op == "create_board"):
            event["principles"] = item.get("principles", [])
        cursor = db.execute("INSERT INTO task_events(plan_id,task_id,body) VALUES(?,?,?)",
                            (item["id"] if board else event["plan_id"], event["task_id"], _dump(event)))
        if plan or board:
            db.execute("DELETE FROM task_events WHERE plan_id=? AND task_id IS NULL AND id NOT IN "
                       "(SELECT id FROM task_events WHERE plan_id=? AND task_id IS NULL ORDER BY id DESC LIMIT ?)",
                       (item["id"], item["id"], HISTORY_LIMIT))
        else:
            db.execute("DELETE FROM task_events WHERE task_id=? AND id NOT IN "
                       "(SELECT id FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?)",
                       (item["id"], item["id"], HISTORY_LIMIT))
        return cursor.lastrowid

    @staticmethod
    def _events(rows):
        return [dict(json.loads(row["body"]), id=row["id"]) for row in rows]

    def snapshot(self, plan_id=None, board_id=None):
        with self._transaction() as db:
            if board_id is not None:
                self._board(db, board_id)
            if plan_id is not None:
                plan = self._plan(db, plan_id)
                if board_id is not None and board_id != plan["board_id"]:
                    raise TaskError("plan does not belong to board", 400)
                board_id = plan["board_id"]
            boards = [json.loads(row["body"]) for row in db.execute("SELECT body FROM task_boards ORDER BY rowid")]
            plans = [json.loads(row["body"]) for row in db.execute("SELECT body FROM task_plans ORDER BY rowid")]
            plans = [plan for plan in plans if (plan_id is None or plan["id"] == plan_id) and (board_id is None or plan["board_id"] == board_id)]
            plan_ids = {plan["id"] for plan in plans}
            tasks = [json.loads(row["body"]) for row in db.execute("SELECT body FROM task_items ORDER BY rowid")]
            return {"version": 1, "link_kinds": sorted(self.link_kinds),
                    "boards": [board for board in boards if board_id is None or board["id"] == board_id],
                    "plans": plans, "tasks": [self._public(db, task) for task in tasks if task["plan_id"] in plan_ids]}

    def get_task(self, task_id):
        with self._transaction() as db:
            task, _ = self._task(db, task_id)
            events = db.execute("SELECT id,body FROM task_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            return {"task": self._public(db, task), "events": self._events(events)}

    def change_marker(self):
        """A number that changes whenever anything is written, and only then. Every write records an event
        (task writes all go through _save_task), event ids come from AUTOINCREMENT so they are never reused, and
        pruning removes only the oldest events, so the newest id only grows. The board page polls this to stay live."""
        with self._transaction() as db:
            return db.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]

    def export(self):
        with self._transaction() as db:
            return {"version": 1, "archival": True, "history_limit": HISTORY_LIMIT,
                    "boards": [json.loads(r["body"]) for r in db.execute("SELECT body FROM task_boards ORDER BY rowid")],
                    "plans": [json.loads(r["body"]) for r in db.execute("SELECT body FROM task_plans ORDER BY rowid")],
                    "tasks": [self._public(db, json.loads(r["body"])) for r in db.execute("SELECT body FROM task_items ORDER BY rowid")],
                    "events": self._events(db.execute("SELECT id,body FROM task_events ORDER BY id"))}

    def create_board(self, req):
        actor = _request(req, BOARD_FIELDS | {"actor"})
        body = {"source_key": _key(req.get("source_key"), "source_key"),
                "title": _text(req.get("title"), "title", 300, True),
                "description": _text(req.get("description", ""), "description", 20000),
                "scope": _text(req.get("scope", ""), "scope", 200)}
        if "principles" in req:
            body["principles"] = self._principles(req["principles"])
        fingerprint = hashlib.sha256(_dump(body).encode()).hexdigest()
        with self._transaction(True) as db:
            row = db.execute("SELECT body,create_fingerprint FROM task_boards WHERE source_key=?", (body["source_key"],)).fetchone()
            if row:
                if row["create_fingerprint"] != fingerprint:
                    raise TaskError("board source_key already exists with different content", 409)
                return {"board": json.loads(row["body"]), "created": False}
            now = self._now()
            board = dict(body, id="board-" + uuid.uuid4().hex, revision=1, created_at=now, updated_at=now)
            db.execute("INSERT INTO task_boards(id,source_key,body,create_fingerprint) VALUES(?,?,?,?)", (board["id"], board["source_key"], _dump(board), fingerprint))
            self._event(db, board, actor, "create_board", board=True)
            return {"board": board, "created": True}

    def update_board(self, req):
        actor = _request(req, {"id", "revision", "actor", "title", "description", "scope", "principles", "note"})
        note = _text(req.get("note", ""), "note", 4000)
        with self._transaction(True) as db:
            board = self._board(db, req.get("id"))
            self._revision(board, req)
            changes = []
            for field, limit in (("title", 300), ("description", 20000), ("scope", 200)):
                if field in req:
                    value = _text(req[field], field, limit, field == "title")
                    if value != board[field]:
                        board[field] = value
                        changes.append(field)
            if "principles" in req:
                value = self._principles(req["principles"])
                if value != board.get("principles", []):
                    if not note:
                        raise TaskError("principles changes require a note")
                    board["principles"] = value
                    changes.append("principles")
            if not changes:
                raise TaskError("no board changes supplied")
            board["revision"] += 1
            board["updated_at"] = self._now()
            db.execute("UPDATE task_boards SET body=? WHERE id=?", (_dump(board), board["id"]))
            self._event(db, board, actor, "update_board", note, changes, board=True)
            return {"board": board}

    def _new_plan(self, req):
        now = self._now()
        return {"id": "plan-" + uuid.uuid4().hex, "revision": 1,
                "board_id": _text(req.get("board_id"), "board_id", 160, True),
                "source_key": _key(req.get("source_key"), "source_key"),
                "title": _text(req.get("title"), "title", 300, True),
                "description": _text(req.get("description", ""), "description", 20000),
                "scope": _text(req.get("scope", ""), "scope", 200),
                "kind": _text(req.get("kind", "mixed"), "kind", 100, True),
                "sources": self._sources(req.get("sources", [])), "created_at": now, "updated_at": now}

    def create_plan(self, req):
        actor = _request(req, PLAN_FIELDS | {"actor"})
        with self._transaction(True) as db:
            self._board(db, req.get("board_id"))
            plan = self._new_plan(req)
            db.execute("INSERT INTO task_plans(id,board_id,source_key,body) VALUES(?,?,?,?)", (plan["id"], plan["board_id"], plan["source_key"], _dump(plan)))
            self._event(db, plan, actor, "create_plan", plan=True)
            return {"plan": plan}

    def update_plan(self, req):
        actor = _request(req, (PLAN_FIELDS - {"source_key", "board_id"}) | {"actor", "id", "revision", "note"})
        note = _text(req.get("note", ""), "note", 4000)
        with self._transaction(True) as db:
            plan = self._plan(db, req.get("id"))
            self._revision(plan, req)
            changed = []
            old_sources = plan["sources"]
            for field in ("title", "description", "scope", "kind", "sources"):
                if field not in req:
                    continue
                value = self._sources(req[field]) if field == "sources" else _text(req[field], field, {"title": 300, "description": 20000, "scope": 200, "kind": 100}[field], field in {"title", "kind"})
                if value != plan[field]:
                    plan[field] = value
                    changed.append(field)
            if not changed:
                raise TaskError("no plan changes supplied")
            if "sources" in changed and not note:
                raise TaskError("source correction requires a note")
            plan["revision"] += 1
            plan["updated_at"] = self._now()
            db.execute("UPDATE task_plans SET body=? WHERE id=?", (_dump(plan), plan["id"]))
            self._event(db, plan, actor, "update_plan", note, changed, plan=True)
            if "sources" in changed:
                old_paths = {source["path"] for source in old_sources}
                for row in db.execute("SELECT body FROM task_items WHERE plan_id=?", (plan["id"],)).fetchall():
                    task = json.loads(row["body"])
                    previous = task["status"]
                    own = [source for source in task["sources"] if source["path"] not in old_paths]
                    task["sources"] = self._merge_sources(plan["sources"], own)
                    self._correct(task)
                    self._save_task(db, task, None, actor, "plan_source_correction", note, ["sources", "evidence"], previous)
            return {"plan": plan}

    @staticmethod
    def _acceptance(value):
        items = [_text(item, "acceptance criterion", 2000, True) for item in _list(value, "acceptance")]
        if not items:
            raise TaskError("acceptance needs at least one criterion")
        return items

    def _links(self, values):
        result = []
        for item in _list(values, "links"):
            if not isinstance(item, dict) or set(item) - {"kind", "ref", "label"}:
                raise TaskError("links require kind, ref and optional label")
            kind = _text(item.get("kind"), "link kind", 20, True)
            if kind not in self.link_kinds:
                raise TaskError("unsupported link kind")
            ref = _text(item.get("ref"), "link ref", 2048, True)
            if any(ord(char) < 32 for char in ref):
                raise TaskError("link ref contains control characters")
            if kind == "url":
                try:
                    url = urlsplit(ref)
                    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
                        raise ValueError()
                except ValueError as exc:
                    raise TaskError("URL links require http(s) without credentials") from exc
            elif kind == "file":
                self._path(ref)
            elif kind == "task":
                _key(ref, "link ref")
            # Any other kind a project registered is an opaque reference (`owner/repo#12`, a SHA...).
            result.append({"kind": kind, "ref": ref, "label": _text(item.get("label", ""), "link label", 300)})
        return result

    def _validate_links(self, db, task):
        for link in task["links"]:
            if link["kind"] == "task":
                self._task(db, link["ref"])

    @staticmethod
    def _tags(values):
        result = [_text(tag, "tag", 64, True) for tag in _list(values, "tags", 30)]
        if len(set(result)) != len(result):
            raise TaskError("duplicate tags")
        return result

    @staticmethod
    def _evidence(values, criteria):
        result = []
        for item in _list(values, "evidence", 200):
            if not isinstance(item, dict) or set(item) - {"criterion", "ref", "note"}:
                raise TaskError("evidence requires criterion, ref and optional note")
            result.append({"criterion": _int(item.get("criterion"), "criterion", 0, len(criteria) - 1),
                           "ref": _text(item.get("ref"), "evidence ref", 2048, True),
                           "note": _text(item.get("note", ""), "evidence note", 4000)})
        return result

    @staticmethod
    def _depends(values):
        result = [_text(item, "dependency id", 160, True) for item in _list(values, "depends_on")]
        if len(set(result)) != len(result):
            raise TaskError("duplicate dependency")
        return result

    def _new_task(self, req, plan, task_id=None):
        criteria = self._acceptance(req.get("acceptance"))
        status = _text(req.get("status", "backlog"), "status", 20, True)
        if status not in {"backlog", "ready"}:
            raise TaskError("new tasks must start in backlog or ready")
        if req.get("blocked_reason", ""):
            raise TaskError("new tasks cannot have a blocked reason")
        now = self._now()
        return {"id": task_id or "task-" + uuid.uuid4().hex, "revision": 1, "plan_id": plan["id"],
                "board_id": plan["board_id"],
                "key": _key(req.get("key")), "title": _text(req.get("title"), "title", 300, True),
                "outcome": _text(req.get("outcome"), "outcome", 8000, True), "acceptance": criteria,
                "depends_on": self._depends(req.get("depends_on", [])),
                "priority": _int(req.get("priority", 2), "priority", 0, 3), "status": status,
                "owner": _text(req.get("owner", ""), "owner", 128),
                "next_step": _text(req.get("next_step", ""), "next_step", 4000), "blocked_reason": "",
                "links": self._links(req.get("links", [])), "evidence": self._evidence(req.get("evidence", []), criteria),
                "sources": self._merge_sources(plan["sources"], self._sources(req.get("sources", []))),
                "attachments": [], "comments": [], "tags": self._tags(req.get("tags", [])),
                "claim_expires_at": None, "created_at": now, "updated_at": now}

    def _validate_graph(self, db, task):
        rows = db.execute("SELECT body FROM task_items WHERE plan_id=?", (task["plan_id"],)).fetchall()
        graph = {value["id"]: value["depends_on"] for value in (json.loads(row["body"]) for row in rows)}
        graph[task["id"]] = task["depends_on"]
        for dependency in task["depends_on"]:
            if dependency not in graph:
                self._task(db, dependency)  # Distinguish unknown from cross-plan references.
                raise TaskError("dependencies must belong to the same plan")
        # Iterative topological elimination avoids Python recursion limits on long plans.
        remaining = {key: set(values) for key, values in graph.items()}
        done = {key for key, values in remaining.items() if not values}
        while done:
            for key in done:
                remaining.pop(key, None)
            next_done = set()
            for key, values in remaining.items():
                values.difference_update(done)
                if not values:
                    next_done.add(key)
            done = next_done
        if remaining:
            raise TaskError("dependency cycle is not allowed")

    def _dependencies_done(self, db, task):
        if any(self._task(db, value)[0]["status"] != "done" for value in task["depends_on"]):
            raise TaskError("dependencies must be done first", 409)

    def create_task(self, req):
        actor = _request(req, TASK_FIELDS | {"actor", "plan_id"})
        with self._transaction(True) as db:
            plan = self._plan(db, req.get("plan_id"))
            task = self._new_task(req, plan)
            self._validate_graph(db, task)
            self._validate_links(db, task)
            if task["status"] == "ready":
                self._dependencies_done(db, task)
            db.execute("INSERT INTO task_items(id,plan_id,task_key,body) VALUES(?,?,?,?)", (task["id"], plan["id"], task["key"], _dump(task)))
            self._event(db, task, actor, "create_task")
            return {"task": self._public(db, task)}

    def import_plan(self, req):
        actor = _request(req, PLAN_FIELDS | {"actor", "tasks"})
        source_key = _key(req.get("source_key"), "source_key")
        fingerprint = hashlib.sha256(_dump({key: value for key, value in req.items() if key != "actor"}).encode()).hexdigest()
        with self._transaction(True) as db:
            self._board(db, req.get("board_id"))
            existing = db.execute("SELECT id,body,import_fingerprint FROM task_plans WHERE board_id=? AND source_key=?", (req["board_id"], source_key)).fetchone()
            if existing:
                if existing["import_fingerprint"] != fingerprint:
                    raise TaskError("source_key already exists with different import content", 409)
                tasks = [self._public(db, json.loads(row["body"])) for row in db.execute("SELECT body FROM task_items WHERE plan_id=? ORDER BY rowid", (existing["id"],))]
                return {"plan": json.loads(existing["body"]), "tasks": tasks, "imported": False}
            plan = self._new_plan(req)
            rows = _list(req.get("tasks"), "tasks", 1000)
            if not rows:
                raise TaskError("import needs at least one task")
            keys = {}
            for row in rows:
                if not isinstance(row, dict) or set(row) - TASK_FIELDS:
                    raise TaskError("import tasks contain unsupported fields")
                key = _key(row.get("key"))
                if key in keys:
                    raise TaskError("duplicate task key in import")
                keys[key] = "task-" + uuid.uuid4().hex
            tasks = []
            for row in rows:
                dependencies = self._depends(row.get("depends_on", []))
                if any(key not in keys for key in dependencies):
                    raise TaskError("unknown local dependency key")
                converted = dict(row, depends_on=[keys[key] for key in dependencies])
                tasks.append(self._new_task(converted, plan, keys[_key(row["key"])]))
            db.execute("INSERT INTO task_plans(id,board_id,source_key,body,import_fingerprint) VALUES(?,?,?,?,?)", (plan["id"], plan["board_id"], source_key, _dump(plan), fingerprint))
            for task in tasks:
                db.execute("INSERT INTO task_items(id,plan_id,task_key,body) VALUES(?,?,?,?)", (task["id"], plan["id"], task["key"], _dump(task)))
            for task in tasks:
                self._validate_graph(db, task)
                self._validate_links(db, task)
                if task["status"] == "ready":
                    self._dependencies_done(db, task)
                self._event(db, task, actor, "import_task")
            self._event(db, plan, actor, "import_plan", plan=True)
            return {"plan": plan, "tasks": [self._public(db, task) for task in tasks], "imported": True}

    def _lease(self, task, lease_hash, req, actor):
        token = _text(req.get("lease", ""), "lease", 200, True)
        actual = hashlib.sha256(token.encode()).hexdigest()
        if (task["status"] != "doing" or task["owner"] != actor or not lease_hash
                or not hmac.compare_digest(actual, lease_hash)):
            raise TaskError("claim lease or owner does not match", 409)
        if task["claim_expires_at"] <= self._now():
            raise TaskError("claim expired; explicitly recover before claiming again", 409)

    def _supplied_lease(self, task, lease_hash, req, actor):
        # An explicit worker token must never degrade into a manual edit after
        # recovery, release, review or correction has revoked that credential.
        if req.get("lease") not in (None, ""):
            self._lease(task, lease_hash, req, actor)

    @staticmethod
    def _correct(task):
        if task["status"] in {"doing", "review", "post_validation", "done"}:
            task["status"] = "backlog"
            task["owner"] = ""
        task["claim_expires_at"] = None
        task["evidence"] = []
        task["checklist_checks"] = []
        task["corrections"] = []

    def _save_task(self, db, task, lease_hash, actor, op, note, changes, previous):
        original, _ = self._task(db, task["id"])
        changes = set(changes) | {field for field in task if original.get(field) != task[field]}
        task["revision"] += 1
        task["updated_at"] = self._now()
        db.execute("UPDATE task_items SET body=?,lease_hash=? WHERE id=?", (_dump(task), lease_hash, task["id"]))
        self._event(db, task, actor, op, note, changes, previous)

    def update_task(self, req):
        actor = _request(req, EDIT_FIELDS | {"id", "revision", "actor", "lease", "note"})
        note = _text(req.get("note", ""), "note", 4000)
        with self._transaction(True) as db:
            task, lease_hash = self._task(db, req.get("id"))
            self._revision(task, req)
            self._supplied_lease(task, lease_hash, req, actor)
            previous = task["status"]
            changes = []
            for field in EDIT_FIELDS - {"evidence", "checklist_checks", "correction_checks"}:
                if field not in req:
                    continue
                if field == "acceptance":
                    value = self._acceptance(req[field])
                elif field == "sources":
                    plan = self._plan(db, task["plan_id"])
                    retained = {source["path"]: source for source in task["sources"]}
                    inherited = [retained.get(source["path"], source) for source in plan["sources"]]
                    value = self._merge_sources(inherited, self._sources(req[field]))
                elif field == "depends_on":
                    value = self._depends(req[field])
                elif field == "priority":
                    value = _int(req[field], field, 0, 3)
                elif field == "links":
                    value = self._links(req[field])
                elif field == "tags":
                    value = self._tags(req[field])
                else:
                    value = _text(req[field], field, {"title": 300, "outcome": 8000, "owner": 128, "next_step": 4000}[field], field in {"title", "outcome"})
                if value != task[field]:
                    task[field] = value
                    changes.append(field)
            correction = bool(set(changes) & CORRECTION_FIELDS)
            if correction:
                if not note:
                    raise TaskError("criteria, outcome, dependency or source correction requires a note")
                self._correct(task)
                lease_hash = None
                changes.append("evidence")
            elif previous == "doing":
                # Validate against the original owner, before assignment edits.
                original, _ = self._task(db, task["id"])
                self._lease(original, lease_hash, req, actor)
                if task["owner"] != original["owner"]:
                    raise TaskError("release a claim before changing its owner", 409)
            if "evidence" in req:
                evidence = self._evidence(req["evidence"], task["acceptance"])
                if correction and evidence:
                    raise TaskError("record new evidence in a later edit after the correction")
                if evidence != task["evidence"]:
                    task["evidence"] = evidence
                    changes.append("evidence")
            if "checklist_checks" in req or "correction_checks" in req:
                if correction:
                    raise TaskError("save scope corrections before new checklist checks")
                if "checklist_checks" in req:
                    values = self._checklist_checks(req["checklist_checks"], task, actor)
                    if values != task.get("checklist_checks", []):
                        task["checklist_checks"] = values
                        changes.append("checklist_checks")
                if "correction_checks" in req:
                    self._correction_checks(req["correction_checks"], task, actor)
                    changes.append("corrections")
            if not changes:
                raise TaskError("no task changes supplied")
            self._validate_graph(db, task)
            self._validate_links(db, task)
            if task["status"] in {"ready", "done"}:
                self._dependencies_done(db, task)
            if task["status"] == "done":
                self._completion(task)
            self._save_task(db, task, lease_hash, actor, "correct_task" if correction else "update_task", note, changes, previous)
            return {"task": self._public(db, task)}

    def attach(self, req):
        """Bind a server-staged immutable upload; never expose as a raw JSON route."""
        actor = _request(req, {"id", "revision", "actor", "lease", "attachment"})
        attachment = req.get("attachment")
        expected = {"id", "filename", "kind", "sha256", "size", "mime", "path"}
        if not isinstance(attachment, dict) or set(attachment) != expected:
            raise TaskError("invalid attachment metadata")
        attachment = dict(attachment)
        attachment["id"] = _key(attachment["id"], "attachment id")
        attachment["filename"] = _text(attachment["filename"], "filename", 255, True)
        if attachment["filename"] in {".", ".."} or any(char in attachment["filename"] for char in "/\\"):
            raise TaskError("attachment filename must be a basename")
        attachment["kind"] = _text(attachment["kind"], "attachment kind", 20, True)
        if attachment["kind"] not in {"asset", "document"}:
            raise TaskError("attachment kind must be asset or document")
        attachment["mime"] = _text(attachment["mime"], "mime", 200, True)
        attachment["size"] = _int(attachment["size"], "size", 0, 32 * 1024 * 1024)
        path = self._path(attachment["path"])
        if not path.startswith(self.assets_prefix):
            raise TaskError(f"attachments must be staged in {self.assets_prefix}")
        actual = self._hash_source(path, 32 * 1024 * 1024)
        if actual["sha256"] != attachment["sha256"] or (self.root / path).stat().st_size != attachment["size"]:
            raise TaskError("attachment fingerprint or size does not match", 409)
        with self._transaction(True) as db:
            task, lease_hash = self._task(db, req.get("id"))
            self._revision(task, req)
            self._supplied_lease(task, lease_hash, req, actor)
            if task["status"] == "doing":
                self._lease(task, lease_hash, req, actor)
            if len(task["attachments"]) >= 100:
                raise TaskError("task attachment limit reached")
            if any(item["id"] == attachment["id"] or item["path"] == path for item in task["attachments"]):
                raise TaskError("attachment id or file already bound", 409)
            attachment.update(created_at=self._now(), author=actor)
            task["attachments"].append(attachment)
            self._save_task(db, task, lease_hash, actor, "attach", attachment["filename"], ["attachments"], task["status"])
            return {"task": self._public(db, task)}

    def comment(self, req):
        actor = _request(req, {"id", "revision", "actor", "lease", "kind", "body", "source_ref"})
        kind = _text(req.get("kind", "note"), "comment kind", 30, True)
        if kind not in {"note", "conversation", "decision"}:
            raise TaskError("unsupported comment kind")
        body = _text(req.get("body"), "comment body", 12000, True)
        source_ref = _text(req.get("source_ref", ""), "source_ref", 2048)
        with self._transaction(True) as db:
            task, lease_hash = self._task(db, req.get("id"))
            self._revision(task, req)
            self._supplied_lease(task, lease_hash, req, actor)
            if task["status"] == "doing":
                self._lease(task, lease_hash, req, actor)
            if len(task["comments"]) >= 100:
                raise TaskError("task comment limit reached; use a linked document for longer records")
            task["comments"].append({"id": "comment-" + uuid.uuid4().hex, "author": actor,
                                     "kind": kind, "body": body, "source_ref": source_ref, "created_at": self._now()})
            self._save_task(db, task, lease_hash, actor, "comment", "", ["comments"], task["status"])
            return {"task": self._public(db, task)}

    def _checklist_checks(self, values, task, actor):
        result, seen = [], set()
        prior = {v["criterion"]: v for v in task.get("checklist_checks", [])}
        for item in _list(values, "checklist_checks"):
            if not isinstance(item, dict) or set(item) != {"criterion", "checked"} or type(item["checked"]) is not bool:
                raise TaskError("checklist checks require criterion and boolean checked")
            i = _int(item["criterion"], "criterion", 0, len(task["acceptance"]) - 1)
            if i in seen:
                raise TaskError("duplicate checklist criterion")
            seen.add(i)
            if item["checked"] and not any(e["criterion"] == i and e["ref"] and e["note"] for e in task["evidence"]):
                raise TaskError("checked criteria require evidence with a result reference and explanation")
            if prior.get(i, {}).get("checked") == item["checked"]:
                result.append(prior[i])
            else:
                result.append(dict(item, actor=actor, at=self._now()))
        return sorted(result, key=lambda v: v["criterion"])

    def _correction_checks(self, values, task, actor):
        corrections = {c["id"]: c for c in task.get("corrections", [])}
        seen = set()
        for value in _list(values, "correction_checks"):
            if not isinstance(value, dict) or set(value) != {"id", "checked", "note", "ref"} or type(value["checked"]) is not bool:
                raise TaskError("correction checks require id, checked, note and ref")
            ident = _text(value["id"], "correction id", 160, True)
            if ident not in corrections or ident in seen:
                raise TaskError("unknown or duplicate correction")
            seen.add(ident)
            note = _text(value["note"], "correction verification", 4000, value["checked"])
            ref = _text(value["ref"], "correction evidence ref", 2048, value["checked"])
            corrections[ident].update(checked=value["checked"], verification=note, verification_ref=ref,
                                      checked_by=actor, checked_at=self._now())

    def _execution_complete(self, task):
        self._completion(task)
        checked = {v["criterion"] for v in task.get("checklist_checks", []) if v["checked"]}
        if checked != set(range(len(task["acceptance"]))):
            raise TaskError("Post validation requires every execution checklist item checked", 409)
        if any(not c.get("checked") for c in task.get("corrections", [])):
            raise TaskError("finish and verify every correction checklist item before Post validation", 409)

    def _validate_result(self, db, task, req, actor):
        value = req.get("validation")
        if not isinstance(value, dict) or set(value) != {"result", "summary", "checks", "principles"}:
            raise TaskError("validation requires result, summary, criterion checks and principles checks")
        if value["result"] not in {"pass", "fail"}:
            raise TaskError("validation result must be pass or fail")
        summary = _text(value["summary"], "validation summary", 4000, True)
        checks, seen = [], set()
        for item in _list(value["checks"], "validation checks"):
            allowed = {"criterion", "passed", "observed", "ref", "expected", "avoid", "fix"}
            if not isinstance(item, dict) or set(item) - allowed or type(item.get("passed")) is not bool:
                raise TaskError("validation checks need criterion, passed, observed and ref; failures also need expected, avoid and fix")
            i = _int(item.get("criterion"), "criterion", 0, len(task["acceptance"]) - 1)
            if i in seen:
                raise TaskError("duplicate validation criterion")
            seen.add(i)
            row = {"criterion": i, "passed": item["passed"]}
            for field, limit in [("observed", 4000), ("ref", 2048), ("expected", 4000), ("avoid", 4000), ("fix", 4000)]:
                row[field] = _text(item.get(field, ""), field, limit, field in {"observed", "ref"} or not item["passed"])
            checks.append(row)
        if seen != set(range(len(task["acceptance"]))):
            raise TaskError("validation must inspect every acceptance criterion")
        context = self._principles_context(db, task)
        expected_keys = {p["key"] for p in context["principles"]}
        principle_checks, principle_seen = [], set()
        for item in _list(value["principles"], "principle validation checks", 24):
            allowed = {"key", "passed", "observed", "ref", "expected", "avoid", "fix"}
            if not isinstance(item, dict) or set(item) - allowed or type(item.get("passed")) is not bool:
                raise TaskError("principle checks require key, passed, observed and ref; failures need expected, avoid and fix")
            key = _key(item.get("key"), "principle key")
            if key not in expected_keys or key in principle_seen:
                raise TaskError("unknown or duplicate principle validation key")
            principle_seen.add(key)
            row = {"principle_key": key, "passed": item["passed"]}
            for field, limit in [("observed", 4000), ("ref", 2048), ("expected", 4000), ("avoid", 4000), ("fix", 4000)]:
                row[field] = _text(item.get(field, ""), field, limit, field in {"observed", "ref"} or not item["passed"])
            principle_checks.append(row)
        if principle_seen != expected_keys:
            raise TaskError("Post validation must inspect every current binding principle", 409)
        failures = [c for c in checks + principle_checks if not c["passed"]]
        if (value["result"] == "fail") != bool(failures):
            raise TaskError("validation result conflicts with criterion checks")
        result = {"id": "validation-" + uuid.uuid4().hex, "result": value["result"], "summary": summary,
                  "checks": checks, "principles": principle_checks, "principles_snapshot": context["principles"], "actor": actor, "at": self._now(), "task_revision": task["revision"],
                  "principles_sha256": task.get("principles_acknowledgement", {}).get("sha256")}
        task["validations"] = (task.get("validations", []) + [result])[-50:]
        if failures:
            corrections = task.setdefault("corrections", [])
            if len(corrections) + len(failures) > 200:
                raise TaskError("correction history limit reached; record a scoped follow-up task")
            for row in failures:
                corrections.append(dict(row, id="correction-" + uuid.uuid4().hex, checked=False,
                                        validation_id=result["id"], verification="", verification_ref=""))
            failed = {c["criterion"] for c in failures if "criterion" in c}
            for mark in task.get("checklist_checks", []):
                if mark["criterion"] in failed:
                    mark.update(checked=False, actor=actor, at=self._now())
            task["status"] = "review"
            task["next_step"] = "Post validation found corrections against the task or binding principles. Read the correction checklist, claim the repair, then rerun both sets of checks."
        else:
            task["status"] = "done"
            task["next_step"] = "Post validation passed against every criterion; no corrective work remains."
        task["claim_expires_at"] = None
        return result

    def _completion(self, task):
        if self._changed(task["sources"]):
            raise TaskError("source documents changed; reconcile with a note first", 409)
        if {item["criterion"] for item in task["evidence"]} != set(range(len(task["acceptance"]))):
            raise TaskError("done requires evidence for every acceptance criterion", 409)

    def action(self, req):
        actor = _request(req, {"id", "revision", "actor", "op", "lease", "lease_seconds", "status", "note", "principles_sha256", "validation"})
        op = _text(req.get("op"), "op", 30, True)
        note = _text(req.get("note", ""), "note", 4000)
        if op not in {"claim", "renew", "release", "recover", "transition", "reconcile", "acknowledge_principles", "validate"}:
            raise TaskError("unknown task operation")
        with self._transaction(True) as db:
            task, lease_hash = self._task(db, req.get("id"))
            self._revision(task, req)
            self._supplied_lease(task, lease_hash, req, actor)
            previous = task["status"]
            token = None
            changes = []
            if op == "claim":
                seconds = _int(req.get("lease_seconds", 900), "lease_seconds", 60, 3600)
                repair = previous == "review" and any(not c.get("checked") for c in task.get("corrections", []))
                if previous != "ready" and not repair:
                    raise TaskError("only ready tasks or failed-validation repairs can be claimed", 409)
                self._dependencies_done(db, task)
                if self._changed(task["sources"]):
                    raise TaskError("source documents changed; reconcile with a note first", 409)
                self._acknowledge_principles(db, task, req, actor, required=bool(self._board(db, task["board_id"]).get("principles")))
                token = secrets.token_urlsafe(32)
                lease_hash = hashlib.sha256(token.encode()).hexdigest()
                task.update(status="doing", owner=actor, claim_expires_at=self._now() + seconds)
                changes = ["status", "owner", "claim_expires_at"]
            elif op == "validate":
                if previous != "post_validation":
                    raise TaskError("validate only tasks in Post validation", 409)
                self._acknowledge_principles(db, task, req, actor)
                self._execution_complete(task)
                self._dependencies_done(db, task)
                validation = self._validate_result(db, task, req, actor)
                note = validation["summary"]
                lease_hash = None
                changes = ["status", "validations", "corrections", "checklist_checks", "next_step"]
            elif op == "acknowledge_principles":
                if previous == "doing":
                    self._lease(task, lease_hash, req, actor)
                self._acknowledge_principles(db, task, req, actor, required=True)
                changes = ["principles_acknowledgement"]
            elif op == "renew":
                self._lease(task, lease_hash, req, actor)
                seconds = _int(req.get("lease_seconds", 900), "lease_seconds", 60, 3600)
                task["claim_expires_at"] = self._now() + seconds
                changes = ["claim_expires_at"]
            elif op == "release":
                self._lease(task, lease_hash, req, actor)
                if not note:
                    raise TaskError("release requires a handoff note")
                task.update(status="ready", owner="", claim_expires_at=None, next_step=note)
                lease_hash = None
                changes = ["status", "owner", "claim_expires_at", "next_step"]
            elif op == "recover":
                if not note:
                    raise TaskError("recovery requires a note")
                if previous != "doing" or task["claim_expires_at"] > self._now():
                    raise TaskError("only an expired doing claim can be recovered", 409)
                task.update(status="ready", owner="", claim_expires_at=None, next_step=note)
                lease_hash = None
                changes = ["status", "owner", "claim_expires_at", "next_step"]
            elif op == "reconcile":
                if not note:
                    raise TaskError("source reconciliation requires a note")
                task["sources"] = self._sources([source["path"] for source in task["sources"]])
                self._correct(task)
                lease_hash = None
                changes = ["sources", "evidence", "claim_expires_at"]
            else:
                status = _text(req.get("status"), "status", 20, True)
                if status not in STATUSES:
                    raise TaskError("unknown task status")
                if status not in TRANSITIONS[previous]:
                    raise TaskError(f"transition from {previous} to {status} is not allowed", 409)
                if status in {"blocked", "cancelled"} or previous in {"done", "cancelled"}:
                    if not note:
                        raise TaskError("blocking, cancellation or reopening requires a note")
                if previous == "doing" and status not in {"blocked", "cancelled"}:
                    self._lease(task, lease_hash, req, actor)
                if status in {"ready", "done"}:
                    self._dependencies_done(db, task)
                if status in {"review", "post_validation", "done"}:
                    self._acknowledge_principles(db, task, req, actor)
                if status == "post_validation":
                    self._execution_complete(task)
                if status == "done":
                    self._completion(task)
                if previous == "done":
                    task["evidence"] = []
                task.update(status=status, blocked_reason=note if status == "blocked" else "", claim_expires_at=None)
                lease_hash = None
                changes = ["status", "blocked_reason", "claim_expires_at"]
            self._save_task(db, task, lease_hash, actor, op, note, changes, previous)
            result = {"task": self._public(db, task)}
            if token is not None:
                result["lease"] = token
            return result
