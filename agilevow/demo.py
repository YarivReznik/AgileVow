"""A board with sample work on it, in one command.

    python -m agilevow demo

Creates ./agilevow-demo, starts the server on it, fills it with a small board and opens the page. Delete the
folder to start over.

The board is filled through the same HTTP API an agent uses, so every card got where it is by the rules:
claimed with a lease, evidence for every criterion, a validation report before Done. Nothing is written to the
database behind the server's back.

Only an empty data directory is filled. Pointing --dir at a folder that already holds boards just serves them,
and a folder that exists but is not an AgileVow folder is refused rather than written into.
"""
import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from . import board as task_board
from .client import Board, BoardError
from .server import DEFAULT_PORT, make_server

DEFAULT_DIR = "agilevow-demo"
NOTES = "notes.md"

PRINCIPLES = [
    {"key": "evidence-first", "title": "Evidence before claims",
     "content": "Every criterion needs something a reviewer can open: a log, a diff, a screenshot. "
                "A statement that it works is not evidence."},
    {"key": "small-changes", "title": "Keep changes small",
     "content": "One task, one reviewable change. If it needs a second concern, make a second task."},
]

# A plan is the shape examples/plan.json has. `status` is where the task starts; _story() then moves some of them.
HARDENING = {
    "source_key": "hardening-sprint", "title": "Hardening sprint", "scope": "backend", "kind": "engineering",
    "description": "Two weeks to get the API ready for the launch.", "sources": [],
    "tasks": [
        {"key": "audit-endpoints", "title": "Audit every public endpoint", "priority": 1, "status": "ready",
         "outcome": "A list of endpoints with their auth and rate-limit status.",
         "acceptance": ["Every route in the router is listed.", "Unauthenticated routes are marked."], "tags": ["security"]},
        {"key": "rate-limit", "title": "Add per-key rate limiting", "priority": 0, "status": "ready",
         "outcome": "Clients are limited to 100 requests a minute and told when they are throttled.",
         "acceptance": ["The 101st request in a minute returns 429.", "The response carries a Retry-After header."], "tags": ["api"]},
        {"key": "retry-policy", "title": "Define the client retry policy", "priority": 2, "status": "ready",
         "outcome": "SDKs retry idempotent calls with backoff and never retry writes.",
         "acceptance": ["The policy is written down.", "The SDK tests cover it."], "tags": ["sdk"]},
        {"key": "fix-flaky-test", "title": "Fix the flaky login test", "priority": 2, "status": "ready",
         "outcome": "The login test passes 50 runs in a row.",
         "acceptance": ["50 consecutive CI runs pass."], "tags": ["ci"]},
        {"key": "error-pages", "title": "Return JSON errors, not HTML", "priority": 2, "status": "ready",
         "outcome": "Every error from the API is a JSON body with a stable code.",
         "acceptance": ["A 404 and a 500 return JSON.", "The error codes are documented."], "tags": ["api"]},
        {"key": "migrate-keys", "title": "Migrate old API keys", "priority": 1, "status": "ready",
         "outcome": "Legacy keys are re-issued with scopes.",
         "acceptance": ["No legacy key remains active."], "tags": ["security"]},
        {"key": "docs-update", "title": "Update the API reference", "priority": 3, "status": "ready",
         "outcome": "The reference matches the shipped behaviour.",
         "acceptance": ["Rate limits are documented.", "Every example was run."], "tags": ["docs"]},
        {"key": "load-test", "title": "Load test at 10x traffic", "priority": 2, "status": "backlog",
         "outcome": "We know where it breaks before customers do.",
         "acceptance": ["A report with the breaking point exists."], "depends_on": ["rate-limit"], "tags": ["perf"]},
    ],
}
LAUNCH = {
    "source_key": "launch-checklist", "title": "Launch checklist", "scope": "demo", "kind": "release",
    "description": "A small plan to try AgileVow on: three tasks, the second and third waiting on the one before.",
    "sources": [],
    "tasks": [
        {"key": "write-readme", "title": "Write the README", "priority": 1, "status": "ready", "tags": ["docs"],
         "outcome": "A newcomer can install the project and run it from the README alone.",
         "acceptance": ["The README has install and run steps that work on a clean machine.",
                        "Every command in the README was run and its output matches what is written."]},
        {"key": "cut-release", "title": "Cut the first release", "status": "backlog", "depends_on": ["write-readme"], "tags": ["release"],
         "outcome": "Version 0.1.0 is tagged and its notes list what changed.",
         "acceptance": ["The tag exists and points at the commit that passed the checks.",
                        "The release notes name every user-visible change."]},
        {"key": "announce", "title": "Announce it", "status": "backlog", "depends_on": ["cut-release"], "tags": ["release"],
         "outcome": "The announcement is published where the first users will see it.",
         "acceptance": ["The post links to the release and to the README."]},
    ],
}
BOARD = {"source_key": "api-hardening", "title": "API hardening", "scope": "backend",
         "description": "Make the public API safe to put in front of customers."}

NOTES_TEXT = """# Sample evidence

This file is the evidence behind the sample cards on the demo board. Evidence is a reference plus a note; a
file inside the project folder becomes a link on the card, so a reviewer can open it.

Nothing here is real work. Delete this folder to start over.
"""

# What the agent wrote against each criterion, for the cards that have evidence.
EVIDENCE = {
    "audit-endpoints": ["Listed every route from the router; the table is in the audit section.",
                        "Marked the three unauthenticated routes."],
    "retry-policy": ["The policy is the first section of the document.", "SDK retry tests pass; the output is attached."],
    "fix-flaky-test": ["50 consecutive green CI runs, listed with their links."],
    "rate-limit": ["Observed the 429 after 100 requests."],
}


def _claim(worker, key, plan):
    task = worker.task(key, plan)
    return worker.claim(task, worker.context(task)["principles_sha256"])


def _evidence(worker, task):
    notes = EVIDENCE[task["key"]]
    return worker.set_evidence(task, *[(i, NOTES, note) for i, note in enumerate(notes)])


def _finish_work(worker, task):
    """Tick every criterion (each has evidence) and hand the card over for review."""
    return worker.action(worker.check(task), "transition", status="review")


def _story(worker, reviewer):
    """Move some cards along so every column has something in it, each step by the rules."""
    # Done: the whole loop, validated by someone else.
    task = _claim(worker, "audit-endpoints", "hardening-sprint")
    task = _finish_work(worker, _evidence(worker, task))
    task = worker.action(task, "transition", status="post_validation")
    digest = reviewer.context(task)["principles_sha256"]
    reviewer.action(task, "validate", principles_sha256=digest, validation={
        "result": "pass",
        "summary": "Opened the evidence for both criteria and both principles; each holds.",
        "checks": [{"criterion": i, "passed": True, "observed": "Opened the evidence and compared it with the router.", "ref": NOTES}
                   for i in range(len(task["acceptance"]))],
        "principles": [{"key": p["key"], "passed": True, "observed": "The evidence is a file a reviewer can open; the change is one diff.", "ref": NOTES}
                       for p in PRINCIPLES]})
    # Post validation: finished, waiting for a reviewer.
    task = _claim(worker, "retry-policy", "hardening-sprint")
    task = _finish_work(worker, _evidence(worker, task))
    worker.action(task, "transition", status="post_validation")
    # Review: work handed over for a look.
    _finish_work(worker, _evidence(worker, _claim(worker, "fix-flaky-test", "hardening-sprint")))
    # Doing: one criterion has evidence, the other does not yet.
    task = _evidence(worker, _claim(worker, "rate-limit", "hardening-sprint"))
    worker.update(task, next_step="Add the Retry-After header, then record evidence for criterion 1.")
    # Blocked: hidden from the board until "show blocked" is switched on, counted in the overview.
    task = _claim(worker, "migrate-keys", "hardening-sprint")
    worker.action(task, "transition", status="blocked", note="Waiting on the customer list from support.")


def seed(url, root):
    """Fill an empty server: one board, two mini projects, eleven tasks spread over the columns."""
    leases = Path(root) / task_board.DATA_DIR / "leases.json"
    worker = Board(actor="agent-1", url=url, leases=leases)
    reviewer = Board(actor="reviewer", url=url, leases=Path(root) / task_board.DATA_DIR / "leases-reviewer.json")
    notes = Path(root) / NOTES
    if not notes.exists():          # never overwrite a file somebody already has
        notes.write_text(NOTES_TEXT, encoding="utf-8")
    board = worker.create_board(BOARD["source_key"], BOARD["title"], BOARD["description"], BOARD["scope"], PRINCIPLES)
    worker.import_plan(board, HARDENING)
    worker.import_plan(board, LAUNCH)
    _story(worker, reviewer)
    return len(HARDENING["tasks"]) + len(LAUNCH["tasks"])


def _prepare(root):
    """The demo folder: new, empty, or one an earlier demo (or any AgileVow server) already used."""
    if root.exists() and not root.is_dir():
        raise task_board.TaskError(f"{root} is a file, not a folder")
    if root.is_dir() and any(root.iterdir()) and not (root / task_board.DATA_DIR).is_dir():
        raise task_board.TaskError(f"{root} already exists and is not an AgileVow folder; choose another with --dir")
    root.mkdir(parents=True, exist_ok=True)


def main(argv=None, stop=None):
    """`stop` is an Event that ends the run (tests set it); without one the demo runs until Ctrl+C."""
    parser = argparse.ArgumentParser(prog="agilevow demo", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=Path(DEFAULT_DIR), help="folder for the demo (default: ./%(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="default: %(default)s")
    parser.add_argument("--no-open", action="store_true", help="do not open the page in a browser")
    args = parser.parse_args(argv)
    root = args.dir.resolve()
    try:
        _prepare(root)
        server = make_server(root, args.port, quiet=True)
    except (task_board.TaskError, RuntimeError) as error:
        print(f"agilevow: {error}", file=sys.stderr)
        return 2
    except OSError as error:  # port already in use, a folder that cannot be written, ...
        print(f"agilevow: cannot start on 127.0.0.1:{args.port}: {error.strerror or error}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Board(actor="agent-1", url=url, leases=root / task_board.DATA_DIR / "leases.json")
        if client.boards():
            note = "It already has boards, so no sample data was added."
        else:
            note = f"{seed(url, root)} sample tasks, from Backlog to Done. Delete the folder to start over."
        print(f"AgileVow demo: {url}\nFolder:        {root}\n{note}\nPress Ctrl+C to stop.", flush=True)
        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception:  # no browser on this machine: the address above is enough
                pass
        stop = stop or threading.Event()
        while not stop.wait(0.5):
            pass
    except BoardError as error:
        print(f"agilevow: could not fill the demo board: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
