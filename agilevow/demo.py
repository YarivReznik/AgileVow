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
from .principles import REPORT_AS_YOU_WORK
from .server import DEFAULT_PORT, make_server

DEFAULT_DIR = "agilevow-demo"

PRINCIPLES = [
    {"key": "evidence-first", "title": "Evidence before claims",
     "content": "Every criterion needs something a reviewer can open: a log, a diff, a screenshot. "
                "A statement that it works is not evidence."},
    {"key": "small-changes", "title": "Keep changes small",
     "content": "One task, one reviewable change. If it needs a second concern, make a second task."},
    REPORT_AS_YOU_WORK,
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

SAMPLE = "Sample evidence for the AgileVow demo board. It is made up; delete the folder to start over.\n\n"
_RUNS = "\n".join(f"run {n:>2}  2026-10-0{1 + n // 10}  login-test  passed  {38 + n % 7}s" for n in range(1, 51))
_REQUESTS = "\n".join(f"{n:>3}  HTTP/1.1 200 OK" for n in (1, 2, 3)) + "\n...\n" + "\n".join(f"{n:>3}  HTTP/1.1 200 OK" for n in (99, 100))
# Each card's evidence is output a reviewer can open and check against the criterion, never a sentence saying it worked.
FILES = {
    "evidence/routes.txt": SAMPLE + """$ app routes --auth
METHOD  PATH                 AUTH     RATE LIMIT
GET     /v1/health           none     none
GET     /v1/status           none     none
POST    /v1/webhooks/stripe  signed   none
GET     /v1/projects         api-key  100/min
POST    /v1/projects         api-key  100/min
GET     /v1/projects/{id}    api-key  100/min
DELETE  /v1/projects/{id}    api-key  100/min

7 routes. Without an API key: /v1/health, /v1/status, /v1/webhooks/stripe (signature checked instead).
""",
    "evidence/retry-policy.md": SAMPLE + """# Client retry policy

- Retry GET, HEAD, PUT and DELETE on 429, 502, 503 and 504, and on connection resets.
- Never retry POST or PATCH unless the request carries an Idempotency-Key.
- Back off 0.5 s, 1 s, 2 s, 4 s with full jitter; stop after 4 tries or 30 s.
- On 429, wait for Retry-After when the server sends it.
""",
    "evidence/sdk-tests.txt": SAMPLE + """$ pytest tests/test_retry.py -q
test_retries_idempotent_get_on_503 PASSED
test_never_retries_post_without_idempotency_key PASSED
test_retries_post_with_idempotency_key PASSED
test_backoff_has_jitter_and_stops_after_four_tries PASSED
test_honours_retry_after_on_429 PASSED
5 passed in 0.84s
""",
    "evidence/ci-runs.txt": SAMPLE + "$ ci runs --job login-test --last 50\n" + _RUNS + "\n\n50 of 50 passed, consecutive.\n",
    "evidence/rate-limit.txt": SAMPLE + "$ for i in $(seq 101); do curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer test-key' localhost:8080/v1/projects; done\n"
                               + _REQUESTS + "\n101  HTTP/1.1 429 Too Many Requests\n     (no Retry-After header yet)\n",
}
# (file, note) per criterion, for the cards that have evidence.
EVIDENCE = {
    "audit-endpoints": [("evidence/routes.txt", "All 7 routes from the router, with their auth and rate limit."),
                        ("evidence/routes.txt", "The three routes without an API key are marked none or signed.")],
    "retry-policy": [("evidence/retry-policy.md", "The written policy."),
                     ("evidence/sdk-tests.txt", "The SDK's retry tests, all passing.")],
    "fix-flaky-test": [("evidence/ci-runs.txt", "The last 50 runs of the login test, all green and consecutive.")],
    "rate-limit": [("evidence/rate-limit.txt", "101 requests in a minute: the 101st returns 429.")],
}


# What the agent said on each card as it worked, as the third principle asks: the plan when it claimed it, then each step.
REPORTS = {
    "audit-endpoints": ["Starting: list every route from the router and mark which ones need an API key.",
                        "Found 7 routes. Health, status and the Stripe webhook take no key; the webhook checks a signature instead."],
    "retry-policy": ["Starting: write the retry policy, then cover it with SDK tests.",
                     "Changed the plan: POST is retried only with an Idempotency-Key, after the review of the payments flow."],
    "fix-flaky-test": ["Starting: the login test fails about one run in twelve. Looking for shared state between tests.",
                       "Cause: the session cookie leaked between tests. Isolated it; 50 runs in a row now pass."],
    "rate-limit": ["Starting: limit each key to 100 requests a minute, in the gateway.",
                   "The 429 works. Still missing: the Retry-After header. Next step updated."],
    "migrate-keys": ["Starting: list the legacy keys and who owns them.",
                     "Blocked: support has the customer list and has not sent it yet."],
}


def _claim(worker, key, plan):
    task = worker.task(key, plan)
    task = worker.claim(task, worker.context(task)["principles_sha256"])
    worker.comment(task, "note", REPORTS[key][0])
    return task


def _report(worker, task):
    return worker.comment(task, "note", REPORTS[task["key"]][1])


def _evidence(worker, task):
    return worker.set_evidence(task, *[(i, ref, note) for i, (ref, note) in enumerate(EVIDENCE[task["key"]])])


def _finish_work(worker, task):
    """Report, tick every criterion (each has evidence) and hand the card over for review."""
    return worker.action(worker.check(_report(worker, task)), "transition", status="review")


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
        "checks": [{"criterion": 0, "passed": True, "observed": "Counted 7 routes in routes.txt and matched them against the router file.", "ref": "evidence/routes.txt"},
                   {"criterion": 1, "passed": True, "observed": "health, status and the Stripe webhook are the only routes without an API key, and each is marked.", "ref": "evidence/routes.txt"}],
        "principles": [{"key": "evidence-first", "passed": True, "observed": "Both criteria point at the captured route table, not at a statement.", "ref": "evidence/routes.txt"},
                       {"key": "small-changes", "passed": True, "observed": "One audit, one file; no unrelated change rode along.", "ref": "evidence/routes.txt"},
                       {"key": "report-as-you-work", "passed": True, "observed": "The card's comments say what was planned when it was claimed and what was found; nothing happened off the record.", "ref": "evidence/routes.txt"}]})
    # Post validation: finished, waiting for a reviewer.
    task = _claim(worker, "retry-policy", "hardening-sprint")
    task = _finish_work(worker, _evidence(worker, task))
    worker.action(task, "transition", status="post_validation")
    # Review: work handed over for a look.
    _finish_work(worker, _evidence(worker, _claim(worker, "fix-flaky-test", "hardening-sprint")))
    # Doing: one criterion has evidence, the other does not yet.
    task = _evidence(worker, _claim(worker, "rate-limit", "hardening-sprint"))
    worker.update(_report(worker, task), next_step="Add the Retry-After header, then record evidence for criterion 1.")
    # Blocked: hidden from the board until "show blocked" is switched on, counted in the overview.
    task = _report(worker, _claim(worker, "migrate-keys", "hardening-sprint"))
    worker.action(task, "transition", status="blocked", note="Waiting on the customer list from support.")


def seed(url, root):
    """Fill an empty server: one board, two mini projects, eleven tasks spread over the columns."""
    leases = Path(root) / task_board.DATA_DIR / "leases.json"
    lead = Board(actor="lead", url=url, leases=Path(root) / task_board.DATA_DIR / "leases-lead.json")   # plans: sets the criteria
    worker = Board(actor="agent-1", url=url, leases=leases)
    reviewer = Board(actor="reviewer", url=url, leases=Path(root) / task_board.DATA_DIR / "leases-reviewer.json")
    for name, text in FILES.items():
        path = Path(root) / name
        if not path.exists():       # never overwrite a file somebody already has
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    board = lead.create_board(BOARD["source_key"], BOARD["title"], BOARD["description"], BOARD["scope"], PRINCIPLES)
    lead.import_plan(board, HARDENING)
    lead.import_plan(board, LAUNCH)
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
