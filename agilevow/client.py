"""A client for a running AgileVow server: a Python API (`Board`) and a command line.

    python -m agilevow client tasks --board my-board
    python -m agilevow client context TASK          # read this first: the principles and their hash
    python -m agilevow client claim TASK --ack-principles HASH

The traps of the procedure (docs/API.md) are encoded here so that every agent does not
rediscover them:

* a mutation carries the record's CURRENT `revision`, so every helper re-reads first;
* `checklist_checks` and `evidence` REPLACE their whole arrays, so `set_evidence` merges into
  what is there and strips read-only fields down to exactly `{criterion, ref, note}`;
* a claim expires (default 3600 s here, 900 s on the server), so `claim` asks for a long lease;
  `renew` extends it, and the lease token is kept on disk because the server shows it once;
* Done needs Post validation; this client never marks anything Done;
* a board with principles refuses a claim until its `principles_sha256` is supplied. The client
  does NOT fetch that hash for you: `context` shows the text and the hash together, on purpose.

Environment: AGILEVOW_URL (default http://127.0.0.1:8765), AGILEVOW_ACTOR (default "agent"),
AGILEVOW_LEASES (default ./.agilevow/leases.json, owner-only).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"


class BoardError(RuntimeError):
    pass


class Board:
    def __init__(self, actor=None, url=None, leases=None):
        self.actor = actor or os.environ.get("AGILEVOW_ACTOR") or "agent"
        self.url = (url or os.environ.get("AGILEVOW_URL") or DEFAULT_URL).rstrip("/")
        # Lease tokens are private and shown ONCE (at claim/renew). A new process can still write to a
        # task it claimed; losing one means waiting out the lease and using `recover`.
        self.leases_path = Path(leases or os.environ.get("AGILEVOW_LEASES") or Path.cwd() / ".agilevow" / "leases.json")
        try:
            self.leases = json.loads(self.leases_path.read_text())
        except (OSError, ValueError):
            self.leases = {}

    def _call(self, method, route, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + route, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise BoardError(f"{method} {route}: HTTP {e.code} {e.read().decode()[:600]}") from None
        except urllib.error.URLError as e:
            raise BoardError(f"cannot reach {self.url}: {e.reason}. Is `python -m agilevow serve` running?") from None

    def _save_leases(self):
        self.leases_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.leases_path.with_name(self.leases_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.leases, stream)
        os.replace(tmp, self.leases_path)

    # ---- reads
    def snapshot(self, board=None):
        query = "" if board is None else "?" + urllib.parse.urlencode({"board": board["id"]})
        return self._call("GET", "/api/tasks" + query)

    def boards(self):
        return self._call("GET", "/api/boards")["boards"]

    def board(self, source_key):
        found = next((b for b in self.boards() if b["source_key"] == source_key), None)
        if found is None:
            raise BoardError(f"no board {source_key!r}")
        return found

    def plan(self, source_key):
        found = next((p for p in self.snapshot()["plans"] if p["source_key"] == source_key), None)
        if found is None:
            raise BoardError(f"no plan {source_key!r}")
        return found

    def task(self, key_or_id, plan_key=None):
        snap = self.snapshot()
        plans = {p["id"]: p for p in snap["plans"]}
        matches = [t for t in snap["tasks"]
                   if t["id"] == key_or_id or (t["key"] == key_or_id and (plan_key is None or plans[t["plan_id"]]["source_key"] == plan_key))]
        if not matches:
            raise BoardError(f"no task {key_or_id!r}")
        if len(matches) > 1:
            raise BoardError(f"{key_or_id!r} is the key of {len(matches)} tasks in different plans; pass the task id or --plan")
        return matches[0]

    def fresh(self, task):
        return self._call("GET", "/api/tasks/item?" + urllib.parse.urlencode({"id": task["id"]}))["task"]

    def context(self, task):
        """What an agent must read before claiming: the principles, their text and their hash."""
        return self.fresh(task)["understanding_context"]

    # ---- writes
    def create_board(self, source_key, title, description="", scope="", principles=()):
        return self._call("POST", "/api/boards/create", {
            "actor": self.actor, "source_key": source_key, "title": title, "description": description,
            "scope": scope, "principles": list(principles)})["board"]

    def import_plan(self, board, plan):
        return self._call("POST", "/api/plans/import", {"actor": self.actor, "board_id": board["id"], **plan})

    def create_plan(self, board, source_key, title, description, kind, sources):
        return self._call("POST", "/api/plans/create", {"actor": self.actor, "board_id": board["id"], "source_key": source_key, "title": title, "description": description, "kind": kind, "sources": sources})["plan"]

    def create_task(self, plan, key, title, outcome, acceptance, **more):
        body = {"actor": self.actor, "plan_id": plan["id"], "key": key, "title": title, "outcome": outcome, "acceptance": acceptance}
        body.update(more)
        return self._call("POST", "/api/tasks/create", body)["task"]

    def comment(self, task, kind, body, source_ref=""):
        t = self.fresh(task)
        req = {"actor": self.actor, "id": t["id"], "revision": t["revision"], "kind": kind, "body": body, "source_ref": source_ref}
        if t["status"] == "doing" and t["id"] in self.leases:
            req["lease"] = self.leases[t["id"]]
        return self._call("POST", "/api/tasks/comment", req)["task"]

    def action(self, task, op, **more):
        t = self.fresh(task)
        req = {"actor": self.actor, "id": t["id"], "revision": t["revision"], "op": op}
        if op in ("renew", "release", "transition") and t["id"] in self.leases:
            req["lease"] = self.leases[t["id"]]
        req.update(more)
        resp = self._call("POST", "/api/tasks/action", req)
        # the token is a SIBLING of `task` in the reply, not a field of it: reading only ["task"] loses it for good
        if resp.get("lease"):
            self.leases[t["id"]] = resp["lease"]
            self._save_leases()
        if op in ("release", "recover") or resp["task"]["status"] != "doing":
            if self.leases.pop(t["id"], None) is not None:
                self._save_leases()
        return resp["task"]

    def claim(self, task, principles_sha256=None, seconds=3600):
        """Claim a ready task. A board with principles also needs `principles_sha256` from `context`."""
        more = {} if principles_sha256 is None else {"principles_sha256": principles_sha256}
        return self.action(task, "claim", lease_seconds=seconds, **more)

    def update(self, task, **fields):
        t = self.fresh(task)
        req = {"actor": self.actor, "id": t["id"], "revision": t["revision"]}
        req.update(fields)
        if t["status"] == "doing" and t["id"] in self.leases:
            req["lease"] = self.leases[t["id"]]
        return self._call("POST", "/api/tasks/update", req)["task"]

    def set_evidence(self, task, *entries):
        """Merge evidence entries (criterion, ref, note) into what the task already holds; replace the same criterion+ref."""
        t = self.fresh(task)
        keep = [{"criterion": e["criterion"], "ref": e["ref"], "note": e.get("note", "")} for e in t.get("evidence", [])]
        for c, ref, note in entries:
            keep = [e for e in keep if not (e["criterion"] == c and e["ref"] == ref)]
            keep.append({"criterion": c, "ref": ref, "note": note})
        return self.update(t, evidence=keep)

    def check(self, task, criteria=None):
        """Tick acceptance criteria (all of them by default). The server only accepts a tick for a criterion that
        already has evidence with a reference and a note. Ticks already there are kept: the array is replaced whole."""
        t = self.fresh(task)
        ticked = {c["criterion"] for c in t.get("checklist", []) if c.get("checked")}
        wanted = set(range(len(t["acceptance"]))) if criteria is None else set(criteria)
        return self.update(t, checklist_checks=[{"criterion": i, "checked": i in ticked or i in wanted}
                                                for i in range(len(t["acceptance"]))])


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except OSError as error:
        raise BoardError(f"cannot read {path}: {error.strerror or error}") from None
    except ValueError as error:
        raise BoardError(f"{path} is not valid JSON: {error}") from None


def _line(task):
    return f"{task['id']}  {task['status']:<15} {task['key']:<24} {task['title']}"


def build_parser():
    p = argparse.ArgumentParser(prog="agilevow client", description=__doc__.split("\n\n")[0])
    p.add_argument("--url", help="server address (default: $AGILEVOW_URL or %s)" % DEFAULT_URL)
    p.add_argument("--actor", help="who is acting (default: $AGILEVOW_ACTOR or 'agent')")
    p.add_argument("--plan", help="plan key, to disambiguate a task key used in several plans")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("boards", help="list boards")
    c = sub.add_parser("create-board", help="create a board (idempotent for the same content)")
    c.add_argument("key"); c.add_argument("title")
    c.add_argument("--description", default=""); c.add_argument("--scope", default="")
    c.add_argument("--principles", type=Path, help="JSON file: a list of {key, title, content}")
    c = sub.add_parser("import", help="import a plan file (see examples/plan.json) into a board")
    c.add_argument("plan", type=Path); c.add_argument("--board", required=True, help="board key")
    c = sub.add_parser("tasks", help="list tasks")
    c.add_argument("--board", help="board key")
    c = sub.add_parser("context", help="print the principles and task context to read before claiming, with the hash")
    c.add_argument("task")
    c = sub.add_parser("claim", help="claim a ready task (needs --ack-principles when the board has principles)")
    c.add_argument("task"); c.add_argument("--ack-principles", dest="sha", metavar="HASH")
    c.add_argument("--seconds", type=int, default=3600)
    c = sub.add_parser("renew", help="extend your lease on a task")
    c.add_argument("task"); c.add_argument("--seconds", type=int, default=3600, help="new lease length, 60 to 3600 (default: %(default)s)")
    c = sub.add_parser("release", help="give a task back, with a note for whoever picks it up")
    c.add_argument("task"); c.add_argument("--note", required=True)
    c = sub.add_parser("check", help="tick acceptance criteria that already have evidence (all, or the 0-based indexes given)")
    c.add_argument("task"); c.add_argument("criteria", nargs="*", type=int)
    c = sub.add_parser("move", help="transition a task you hold: review, post_validation, blocked, ...")
    c.add_argument("task"); c.add_argument("status"); c.add_argument("--note", default="")
    c = sub.add_parser("comment", help="add a comment")
    c.add_argument("task"); c.add_argument("text")
    c.add_argument("--kind", choices=("note", "conversation", "decision"), default="note")
    c = sub.add_parser("evidence", help="record evidence for one acceptance criterion (0-based index)")
    c.add_argument("task"); c.add_argument("criterion", type=int); c.add_argument("ref")
    c.add_argument("--note", default="")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    board = Board(actor=args.actor, url=args.url)
    try:
        if args.command == "boards":
            for b in board.boards():
                print(f"{b['id']}  {b['source_key']:<24} {b['title']}")
        elif args.command == "create-board":
            principles = _read_json(args.principles) if args.principles else []
            b = board.create_board(args.key, args.title, args.description, args.scope, principles)
            count = len(b.get("principles", []))
            print(f"{b['id']}  {b['source_key']}  ({count} principle{'' if count == 1 else 's'})")
        elif args.command == "import":
            result = board.import_plan(board.board(args.board), _read_json(args.plan))
            print(f"{'imported' if result['imported'] else 'already imported'}: {result['plan']['id']}  {len(result['tasks'])} tasks")
        elif args.command == "tasks":
            snap = board.snapshot(board.board(args.board) if args.board else None)
            for t in snap["tasks"]:
                print(_line(t))
        else:
            task = board.task(args.task, args.plan)
            if args.command == "context":
                context = board.context(task)
                print(context["text"])
                print(f"\nprinciples_sha256: {context['principles_sha256']}")
            elif args.command == "claim":
                done = board.claim(task, args.sha, args.seconds)
                print(f"claimed {done['id']} for {board.actor}; lease kept in {board.leases_path}")
            elif args.command == "renew":
                print(_line(board.action(task, "renew", lease_seconds=args.seconds)))
            elif args.command == "release":
                print(_line(board.action(task, "release", note=args.note)))
            elif args.command == "check":
                print(_line(board.check(task, args.criteria or None)))
            elif args.command == "move":
                print(_line(board.action(task, "transition", status=args.status, note=args.note)))
            elif args.command == "comment":
                print(_line(board.comment(task, args.kind, args.text)))
            elif args.command == "evidence":
                print(_line(board.set_evidence(task, (args.criterion, args.ref, args.note))))
    except BoardError as error:
        print(f"agilevow: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
