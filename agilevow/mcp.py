"""An MCP server for AgileVow: the worker loop as tools, over stdio.

    python -m agilevow mcp [--url URL] [--actor NAME] [--leases FILE]

It speaks the Model Context Protocol (JSON-RPC 2.0, one message per line on stdin and stdout) and is a thin client of
a running `agilevow serve`. Every tool goes through the same HTTP API, and so the same checks, as the command line and
the web board; there is no second way to change a card. Standard library only.

Deliberately absent: creating a board. A board's binding principles are the user's requirements, so an agent may read
them and plan inside the board (import_plan) but does not get to invent them. Lease tokens are never returned to the
model; they stay in an owner-only file.
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

from . import __version__, manager, review
from .client import DEFAULT_URL, Board, BoardError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
MAX_TEXT = 100_000
MOVES = ("backlog", "ready", "review", "post_validation", "blocked", "cancelled")

INSTRUCTIONS = """AgileVow is a task board with a strict workflow. Follow it exactly.
1. Resume first: list_tasks (and audit_board). Continue a card you already hold before starting another.
2. get_context before claiming: it holds the board's binding principles and their hash. Read them, then claim_task with that principles_sha256.
3. Work, then record_evidence for every acceptance criterion (numbered from 0) using real results only, then check_criteria.
4. move_task to review or post_validation. There is no move to done and no workaround.
5. A card is Done only through prepare_review, then inspecting the work and filling every row honestly, then submit_review. A failure returns the card to review with a repair list.
6. release_task with a note before you switch to other work. Never invent or reconstruct evidence."""


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ToolInputError(ValueError):
    pass


# ---- input validation: the small subset of JSON Schema that the tools below use -------------------------------------
def _check(value, schema, where="arguments"):
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ToolInputError(f"{where} must be an object")
        for name in schema.get("required", ()):
            if name not in value:
                raise ToolInputError(f"{where}.{name} is required")
        props = schema.get("properties", {})
        for name, item in value.items():
            if name not in props:
                if schema.get("additionalProperties") is False:
                    raise ToolInputError(f"{where}.{name} is not a known argument (known: {', '.join(props) or 'none'})")
                continue
            _check(item, props[name], f"{where}.{name}")
    elif kind == "string":
        if not isinstance(value, str):
            raise ToolInputError(f"{where} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolInputError(f"{where} must be one of: {', '.join(schema['enum'])}")
    elif kind == "integer":
        if type(value) is not int:
            raise ToolInputError(f"{where} must be an integer")
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{where} must be between {schema.get('minimum')} and {schema.get('maximum')}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ToolInputError(f"{where} must be an array")
        for i, item in enumerate(value):
            _check(item, schema.get("items", {}), f"{where}[{i}]")


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _str(description, **extra):
    return dict(type="string", description=description, **extra)


TASK = _str("Task key (for example write-readme) or task id (task-...).")
PLAN = _str("Plan key. Only needed when the same task key exists in two plans.")
BOARD = _str("Board key or board id.")


# ---- shared helpers -------------------------------------------------------------------------------------------------
def _plans(board):
    return {p["id"]: p["source_key"] for p in board.snapshot()["plans"]}


def _brief(task, plans):
    return {"id": task["id"], "key": task["key"], "plan": plans.get(task["plan_id"]), "title": task["title"],
            "status": task["status"], "priority": task.get("priority"), "owner": task.get("owner") or None}


def _find(board, args):
    return board.task(args["task"], args.get("plan"))


def _api(board):
    """The `api(route, body=None)` shape review.submit expects."""
    return lambda route, body=None: board._call("POST" if body is not None else "GET", route, body)


def _board_by_ref(board, ref):
    boards = board.boards()
    found = next((b for b in boards if ref in (b["id"], b["source_key"])), None)
    if found is None:
        raise BoardError(f"no board {ref!r}; boards on this server: {', '.join(b['source_key'] for b in boards) or 'none yet'}")
    return found


# ---- the tools ------------------------------------------------------------------------------------------------------
def t_list_boards(board, args):
    return [{"key": b["source_key"], "id": b["id"], "title": b["title"], "scope": b.get("scope", ""),
             "principles": len(b.get("principles", []))} for b in board.boards()]


def t_list_tasks(board, args):
    snap = board.snapshot(_board_by_ref(board, args["board"]) if "board" in args else None)
    plans = {p["id"]: p["source_key"] for p in snap["plans"]}
    rows = [_brief(t, plans) for t in snap["tasks"] if "status" not in args or t["status"] == args["status"]]
    return {"count": len(rows), "tasks": rows}


def t_get_task(board, args):
    found = _find(board, args)
    item = board._call("GET", "/api/tasks/item?id=" + found["id"])
    task = dict(item["task"])
    context = task.pop("understanding_context", {})
    task["principles"] = {"sha256": context.get("principles_sha256"), "count": len(context.get("principles", [])),
                          "note": "call get_context to read them before claiming"}
    task["comments"] = task.get("comments", [])[-10:]
    task["recent_events"] = item.get("events", [])[-10:]
    return task


def t_get_context(board, args):
    found = _find(board, args)
    context = board.context(found)
    return {"principles_sha256": context["principles_sha256"], "text": context["text"],
            "next": "Read the text above, then call claim_task with this principles_sha256."}


def t_claim_task(board, args):
    claimed = board.claim(_find(board, args), args.get("principles_sha256"), args.get("seconds", 3600))
    return {"claimed": _brief(claimed, _plans(board)), "owner": board.actor,
            "lease_seconds": args.get("seconds", 3600), "revision": claimed["revision"]}


def t_renew_lease(board, args):
    renewed = board.action(_find(board, args), "renew", lease_seconds=args.get("seconds", 3600))
    return {"task": _brief(renewed, _plans(board)), "claim_expires_at": renewed.get("claim_expires_at")}


def t_release_task(board, args):
    return {"task": _brief(board.action(_find(board, args), "release", note=args["note"]), _plans(board))}


def t_record_evidence(board, args):
    updated = board.set_evidence(_find(board, args), (args["criterion"], args["ref"], args["note"]))
    return {"task": _brief(updated, _plans(board)), "evidence_count": len(updated.get("evidence", []))}


def t_check_criteria(board, args):
    updated = board.check(_find(board, args), args.get("criteria"))
    return {"task": _brief(updated, _plans(board)),
            "checklist": [{"criterion": c["criterion"], "checked": c["checked"]} for c in updated.get("checklist", [])]}


def t_move_task(board, args):
    moved = board.action(_find(board, args), "transition", status=args["status"], note=args.get("note", ""))
    return {"task": _brief(moved, _plans(board))}


def t_add_comment(board, args):
    commented = board.comment(_find(board, args), args.get("kind", "note"), args["text"])
    return {"task": _brief(commented, _plans(board)), "comments": len(commented.get("comments", []))}


def t_audit_board(board, args):
    snap = board.snapshot(_board_by_ref(board, args["board"]))
    return manager.audit(snap["tasks"], int(time.time()), args.get("wip_limit", 1), args.get("stale_seconds", 3600))


def t_prepare_review(board, args):
    return review.prepare(board.fresh(_find(board, args)))


def t_submit_review(board, args):
    found = board.fresh(_find(board, args))
    done = review.submit(_api(board), found, args["report"])
    return {"task": _brief(done, _plans(board)), "status": done["status"],
            "next": "Done." if done["status"] == "done" else "A check failed: the card is back in review with a repair list. Claim it, fix it, and validate again."}


def t_import_plan(board, args):
    result = board.import_plan(_board_by_ref(board, args["board"]), args["plan"])
    plans = {result["plan"]["id"]: result["plan"]["source_key"]}
    return {"imported": result["imported"], "plan": result["plan"]["source_key"],
            "tasks": [_brief(t, plans) for t in result["tasks"]]}


def _tool(name, title, description, schema, run, read_only=False, idempotent=False):
    return {"name": name, "title": title, "description": description, "inputSchema": schema, "run": run,
            "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": False,
                            "idempotentHint": idempotent or read_only, "openWorldHint": False}}


def build_tools():
    return [
        _tool("list_boards", "List boards", "List the boards on the server: key, id, title and how many binding principles each has.",
              _obj({}), t_list_boards, read_only=True),
        _tool("list_tasks", "List tasks", "List tasks, optionally for one board and/or one status. Use this first to resume work.",
              _obj({"board": BOARD, "status": _str("Only tasks in this status.", enum=["backlog", "ready", "doing", "review", "post_validation", "done", "blocked", "cancelled"])}),
              t_list_tasks, read_only=True),
        _tool("get_task", "Get a task", "Read one task in full: outcome, acceptance criteria, evidence, checklist, corrections, links, status, owner, and recent comments and events.",
              _obj({"task": TASK, "plan": PLAN}, ["task"]), t_get_task, read_only=True),
        _tool("get_context", "Read the principles and task context",
              "Read the board's binding principles and the task context, with the principles_sha256. Do this before claim_task: a board with principles refuses a claim that does not carry the current hash.",
              _obj({"task": TASK, "plan": PLAN}, ["task"]), t_get_context, read_only=True),
        _tool("claim_task", "Claim a task",
              "Claim a ready task (or a failed task with unverified corrections) with a lease. When the board has binding principles you must pass the principles_sha256 returned by get_context. The lease token is kept for you; you never see it.",
              _obj({"task": TASK, "plan": PLAN, "principles_sha256": _str("Required when the board has binding principles: the hash from get_context."),
                    "seconds": {"type": "integer", "minimum": 60, "maximum": 3600, "description": "Lease length. Default 3600."}}, ["task"]), t_claim_task),
        _tool("renew_lease", "Renew a lease", "Extend your lease on a task you hold. After expiry you can no longer write to the card.",
              _obj({"task": TASK, "plan": PLAN, "seconds": {"type": "integer", "minimum": 60, "maximum": 3600, "description": "New lease length. Default 3600."}}, ["task"]),
              t_renew_lease),
        _tool("release_task", "Release a task", "Give a task you hold back, with a handoff note that becomes its next step. Do this before switching to other work.",
              _obj({"task": TASK, "plan": PLAN, "note": _str("What was done and where to resume.")}, ["task", "note"]), t_release_task),
        _tool("record_evidence", "Record evidence for a criterion",
              "Record evidence for one acceptance criterion (numbered from 0), merging with what is already recorded. Use real results only: a file path, a URL, command output saved somewhere. A criterion cannot be checked without a reference and a note.",
              _obj({"task": TASK, "plan": PLAN, "criterion": {"type": "integer", "minimum": 0, "description": "Acceptance criterion number, from 0."},
                    "ref": _str("Where the evidence is: a path relative to the project, a URL, or an identifier."),
                    "note": _str("What this evidence shows.")}, ["task", "criterion", "ref", "note"]),
              t_record_evidence, idempotent=True),
        _tool("check_criteria", "Check criteria off", "Tick acceptance criteria that already have evidence: the ones listed (0-based), or all. Ticks already there are kept.",
              _obj({"task": TASK, "plan": PLAN, "criteria": {"type": "array", "items": {"type": "integer", "minimum": 0}, "description": "Criterion numbers to tick. Omit for all."}}, ["task"]),
              t_check_criteria, idempotent=True),
        _tool("move_task", "Move a task",
              "Move a task you hold to review or post_validation, or block, cancel, ready or backlog it (blocking and cancelling need a note). There is no move to done or doing: claim_task starts work and submit_review is the only way to Done.",
              _obj({"task": TASK, "plan": PLAN, "status": _str("Where to move it.", enum=list(MOVES)), "note": _str("Why. Required for blocked and cancelled.")}, ["task", "status"]),
              t_move_task),
        _tool("add_comment", "Add a comment", "Record a note, a conversation excerpt, or a decision on the task. Record decisions and user corrections here.",
              _obj({"task": TASK, "plan": PLAN, "text": _str("The comment."), "kind": _str("Kind of comment. Default note.", enum=["note", "conversation", "decision"])}, ["task", "text"]),
              t_add_comment),
        _tool("audit_board", "Audit a board", "Read-only advice on what to work on next and what is wrong on a board: queues, the next task, stale or expired claims, missing evidence.",
              _obj({"board": BOARD, "wip_limit": {"type": "integer", "minimum": 1, "description": "Default 1."}, "stale_seconds": {"type": "integer", "minimum": 1, "description": "Default 3600."}}, ["board"]),
              t_audit_board, read_only=True),
        _tool("prepare_review", "Prepare a validation report",
              "For a task in post_validation: return a validation report with one pending row per acceptance criterion and per binding principle. Inspect the actual work, then fill every row with what you observed and pass the report to submit_review.",
              _obj({"task": TASK, "plan": PLAN}, ["task"]), t_prepare_review, read_only=True),
        _tool("submit_review", "Submit a validation report",
              "Submit the filled report. Every criterion and every principle needs result pass or fail, a method, what you actually observed, and evidence; a fail also needs expected, avoid and fix. All pass: the card is Done. Any fail: it returns to review with a repair list. Do not validate work you did not inspect.",
              _obj({"task": TASK, "plan": PLAN, "report": {"type": "object", "description": "The report from prepare_review, with reviewer and summary set and every row completed."}}, ["task", "report"]),
              t_submit_review),
        _tool("import_plan", "Import a plan",
              "Plan work on an existing board by importing a plan (title, source_key, tasks with acceptance criteria and optional depends_on keys). Repeating an identical import changes nothing; changed content under the same key is refused.",
              _obj({"board": BOARD, "plan": {"type": "object", "description": "A plan like examples/plan.json: source_key, title, description, scope, kind, sources, tasks."}}, ["board", "plan"]),
              t_import_plan),
    ]


# ---- the protocol ---------------------------------------------------------------------------------------------------
def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _text_result(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n[truncated: {len(text) - MAX_TEXT} more characters]"
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Server:
    def __init__(self, board):
        self.board = board
        self.tools = {tool["name"]: tool for tool in build_tools()}

    def handle(self, message):
        """One decoded JSON-RPC message in, one response (or None for a notification) out."""
        if isinstance(message, list):
            if not message:
                return _error(None, -32600, "Invalid Request")
            replies = [r for r in (self.handle(item) for item in message) if r is not None]
            return replies or None
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        if "method" not in message and ("result" in message or "error" in message):
            return None  # a reply to something we never asked
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(message.get("id"), -32600, "Invalid Request")
        notification = "id" not in message
        try:
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise RpcError(-32602, "params must be an object")
            result = self.dispatch(method, params, notification)
        except RpcError as error:
            return None if notification else _error(message["id"], error.code, error.message)
        except Exception:  # a bug here must not kill the session or leak a traceback to the model
            traceback.print_exc(file=sys.stderr)
            return None if notification else _error(message["id"], -32603, "Internal error")
        return None if notification else {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def dispatch(self, method, params, notification):
        if notification and not method.startswith("notifications/"):
            return {}  # a request sent without an id gets no answer, so it must not run either
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return {"protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agilevow", "title": "AgileVow", "version": __version__},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{key: tool[key] for key in ("name", "title", "description", "inputSchema", "annotations")} for tool in self.tools.values()]}
        if method == "tools/call":
            return self.call_tool(params)
        if method.startswith("notifications/") or notification:
            return {}  # initialized, cancelled, and anything else we do not act on
        raise RpcError(-32601, f"Method not found: {method}")

    def call_tool(self, params):
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise RpcError(-32602, f"Unknown tool: {name!r}")
        arguments = params.get("arguments")
        arguments = {} if arguments is None else arguments
        try:
            _check(arguments, tool["inputSchema"])
            return _text_result(tool["run"](self.board, arguments))
        except (ToolInputError, BoardError, ValueError) as error:  # the model can read these and correct itself
            return _text_result(str(error), is_error=True)
        except KeyError as error:
            return _text_result(f"missing field {error}", is_error=True)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return _text_result("internal error; see the server log", is_error=True)


def serve(server, source, sink):
    """Newline-delimited JSON-RPC until the client closes stdin."""
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "Parse error")
        else:
            response = server.handle(message)
        if response is not None:
            sink.write(json.dumps(response) + "\n")
            sink.flush()


def _default_leases(actor):
    # An MCP client starts this process from a directory of its choosing, so a path relative to the working
    # directory would be arbitrary (or unwritable). Keep one owner-only file per actor under the home folder.
    folder = Path.home() / ".agilevow"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder / f"leases-{re.sub(r'[^A-Za-z0-9._-]', '_', actor)}.json"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agilevow mcp", description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="address of `agilevow serve` (default: $AGILEVOW_URL or %s)" % DEFAULT_URL)
    parser.add_argument("--actor", help="who the agent acts as (default: $AGILEVOW_ACTOR or 'agent')")
    parser.add_argument("--leases", help="lease token file (default: ~/.agilevow/leases-ACTOR.json, owner-only)")
    args = parser.parse_args(argv)
    actor = args.actor or os.environ.get("AGILEVOW_ACTOR") or "agent"
    leases = args.leases or os.environ.get("AGILEVOW_LEASES") or _default_leases(actor)
    board = Board(actor=actor, url=args.url, leases=leases)
    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # stdout belongs to the protocol: a stray print anywhere must not corrupt it
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    print(f"agilevow mcp: ready; board server {board.url}, acting as {actor!r}", file=sys.stderr, flush=True)
    try:
        serve(Server(board), sys.stdin, protocol_out)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
