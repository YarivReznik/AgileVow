"""The MCP server: the protocol, the tool contracts, the whole worker loop to Done, and the real stdio framing."""
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

from agilevow import mcp
from agilevow.client import Board
from agilevow.server import make_server

REPO = Path(__file__).resolve().parent.parent
PLAN = json.loads((REPO / "examples" / "plan.json").read_text())
PRINCIPLES = [{"key": "terse", "title": "Terse", "content": "Say it in one line."}]
READ_ONLY = {"list_boards", "list_tasks", "get_task", "get_context", "audit_board", "prepare_review"}


def rpc(method, params=None, mid=1):
    message = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        message["params"] = params
    return message


class ProtocolTests(unittest.TestCase):
    """Nothing here touches a board, so the server is built without one."""

    def setUp(self):
        self.server = mcp.Server(None)

    def test_initialize_negotiates_a_version_and_describes_itself(self):
        for asked, expected in (("2024-11-05", "2024-11-05"), ("2025-03-26", "2025-03-26"), ("2099-01-01", mcp.PROTOCOL_VERSIONS[0])):
            result = self.server.handle(rpc("initialize", {"protocolVersion": asked, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}))["result"]
            self.assertEqual(result["protocolVersion"], expected)
        self.assertEqual(result["serverInfo"]["name"], "agilevow")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("no move to done", result["instructions"])
        # a second AgileVow server in the same session is not an independent reviewer: the agent is told not to use one
        self.assertIn("Never call another AgileVow server's submit_review", result["instructions"])

    def test_ids_are_echoed_including_zero_and_strings(self):
        for mid in (0, "abc", 7):
            self.assertEqual(self.server.handle(rpc("ping", mid=mid)), {"jsonrpc": "2.0", "id": mid, "result": {}})

    def test_notifications_get_no_answer_and_unknown_requests_get_an_error(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "something/unknown"}))
        self.assertEqual(self.server.handle(rpc("resources/list"))["error"]["code"], -32601)

    def test_malformed_input_is_answered_not_fatal(self):
        out = io.StringIO()
        mcp.serve(self.server, io.StringIO('{broken\n\n{"jsonrpc":"2.0","id":5,"method":"ping"}\n[]\n{"foo":1}\n'), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in replies], [-32700, None, -32600, -32600])
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 5, "result": {}})
        self.assertEqual(replies[0]["id"], None)

    def test_a_batch_is_answered_as_a_batch_without_the_notifications(self):
        reply = self.server.handle([rpc("ping", mid=1), {"jsonrpc": "2.0", "method": "notifications/initialized"}, rpc("ping", mid=2)])
        self.assertEqual([r["id"] for r in reply], [1, 2])
        self.assertIsNone(self.server.handle([{"jsonrpc": "2.0", "method": "notifications/initialized"}]))

    def test_a_reply_to_something_we_never_asked_is_ignored(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "id": 3, "result": {}}))

    def test_every_tool_is_described_completely_and_consistently(self):
        tools = self.server.handle(rpc("tools/list"))["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)))
        for tool in tools:
            self.assertTrue(tool["description"].strip() and tool["title"].strip(), tool["name"])
            schema = tool["inputSchema"]
            self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False), tool["name"])
            self.assertLessEqual(set(schema["required"]), set(schema["properties"]), tool["name"])
            for prop in schema["properties"].values():
                self.assertTrue(prop.get("description") or prop.get("enum"), f"{tool['name']}: a property with no description")
            hints = tool["annotations"]
            self.assertEqual(set(hints) - {"title"}, {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
            self.assertFalse(hints["openWorldHint"])
            self.assertFalse(hints["destructiveHint"])
            self.assertEqual(hints["readOnlyHint"], tool["name"] in READ_ONLY, tool["name"])

    def test_what_the_server_deliberately_does_not_offer(self):
        names = {t["name"] for t in self.server.handle(rpc("tools/list"))["result"]["tools"]}
        self.assertNotIn("update_board", names)  # no tool changes a board's principles: an agent cannot rewrite the rules it is judged against
        for tool in self.server.tools.values():
            self.assertFalse({"lease", "token", "lease_token"} & set(tool["inputSchema"]["properties"]), tool["name"])
        move = self.server.tools["move_task"]["inputSchema"]["properties"]["status"]["enum"]
        self.assertNotIn("done", move)
        self.assertNotIn("doing", move)

    def test_bad_arguments_come_back_as_errors_the_model_can_read(self):
        def call(name, arguments):
            result = self.server.handle(rpc("tools/call", {"name": name, "arguments": arguments}))["result"]
            self.assertTrue(result["isError"], (name, arguments))
            return result["content"][0]["text"]
        self.assertIn("task is required", call("get_task", {}))
        self.assertIn("must be a string", call("get_task", {"task": 5}))
        self.assertIn("not a known argument", call("get_task", {"task": "a", "surprise": 1}))
        self.assertIn("must be one of", call("move_task", {"task": "a", "status": "done"}))  # the product's central rule
        self.assertIn("between 60 and 3600", call("claim_task", {"task": "a", "seconds": 5}))
        self.assertIn("must be an integer", call("record_evidence", {"task": "a", "criterion": "0", "ref": "r", "note": "n"}))
        self.assertIn("must be an array", call("check_criteria", {"task": "a", "criteria": 0}))

    def test_an_unknown_tool_is_a_protocol_error(self):
        self.assertEqual(self.server.handle(rpc("tools/call", {"name": "delete_everything", "arguments": {}}))["error"]["code"], -32602)
        self.assertEqual(self.server.handle(rpc("tools/call", {"arguments": {}}))["error"]["code"], -32602)

    def test_a_huge_result_is_cut_with_a_note_so_it_cannot_flood_the_model(self):
        self.server.tools["list_boards"]["run"] = lambda board, args: "x" * (mcp.MAX_TEXT + 500)
        text = self.server.handle(rpc("tools/call", {"name": "list_boards", "arguments": {}}))["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("x" * 100) and "[truncated: 500 more characters]" in text)
        self.assertLess(len(text), mcp.MAX_TEXT + 100)

    def test_a_tool_call_sent_as_a_notification_does_not_run(self):
        ran = []
        self.server.tools["list_boards"]["run"] = lambda board, args: ran.append(1) or []
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "list_boards", "arguments": {}}}))
        self.assertEqual(ran, [])
        self.assertEqual(self.server.handle(rpc("tools/call", {"name": "list_boards", "arguments": {}}))["result"]["isError"], False)
        self.assertEqual(ran, [1])


class LiveTools(unittest.TestCase):
    """The tools against a real server, in the same process."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "notes.md").write_text("Observed result\n")
        self.http = make_server(self.root, port=0, quiet=True)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.leases = self.root / "mcp-leases.json"
        self.server = mcp.Server(Board(actor="claude", url=self.url, leases=self.leases))
        self.seen = []  # every byte of text the model was shown

    def call(self, name, **arguments):
        result = self.server.handle(rpc("tools/call", {"name": name, "arguments": arguments}))["result"]
        text = result["content"][0]["text"]
        self.seen.append(text)
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        return result["isError"], value

    def ok(self, name, **arguments):
        is_error, value = self.call(name, **arguments)
        self.assertFalse(is_error, f"{name}: {value}")
        return value

    def plan_a_board(self, principles=()):
        Board(actor="lead", url=self.url, leases=self.root / "lead.json").create_board("demo", "Demo board", principles=principles)
        return self.ok("import_plan", board="demo", plan=PLAN)

    def test_the_whole_loop_to_done_through_the_tools(self):
        self.plan_a_board(PRINCIPLES)
        boards = self.ok("list_boards")
        self.assertEqual([{k: v for k, v in b.items() if k != "id"} for b in boards],
                         [{"key": "demo", "title": "Demo board", "scope": "", "principles": 1}])
        self.assertTrue(boards[0]["id"].startswith("board-"))
        listing = self.ok("list_tasks", board="demo", status="ready")
        self.assertEqual([t["key"] for t in listing["tasks"]], ["write-readme"])

        context = self.ok("get_context", task="write-readme")
        self.assertIn("Say it in one line.", context["text"])
        digest = context["principles_sha256"]

        is_error, refusal = self.call("claim_task", task="write-readme")  # no hash: the board says why
        self.assertTrue(is_error)
        self.assertIn("principles_sha256", refusal)
        self.assertEqual(self.ok("get_task", task="write-readme")["status"], "ready")

        self.assertEqual(self.ok("claim_task", task="write-readme", principles_sha256=digest)["claimed"]["status"], "doing")
        (token,) = json.loads(self.leases.read_text()).values()  # the real lease token, which only the file may hold
        held = self.ok("get_task", task="write-readme")
        self.assertEqual(held["owner"], "claude")
        self.assertNotIn("text", held["principles"])  # the long text is get_context's job
        self.assertEqual(held["principles"]["sha256"], digest)

        self.ok("record_evidence", task="write-readme", criterion=0, ref="notes.md", note="Read it")
        self.ok("record_evidence", task="write-readme", criterion=1, ref="notes.md", note="Ran every command")
        ticks = self.ok("check_criteria", task="write-readme")["checklist"]
        self.assertEqual([c["checked"] for c in ticks], [True, True])
        self.ok("add_comment", task="write-readme", text="Kept the install to two commands", kind="decision")

        is_error, _ = self.call("move_task", task="write-readme", status="done")
        self.assertTrue(is_error)  # rejected by the schema before it can even reach the server
        self.ok("move_task", task="write-readme", status="review")
        self.ok("move_task", task="write-readme", status="post_validation")

        report = self.ok("prepare_review", task="write-readme")
        self.assertEqual([len(report["checks"]), len(report["principles"])], [2, 1])
        self.assertEqual(report["reviewer"], "claude")  # over MCP the reviewer is the actor this server runs as
        is_error, blank = self.call("submit_review", task="write-readme", report=report)
        self.assertEqual((is_error, blank), (True, "Validation summary is required"))  # a blank report is refused outright
        report.update(summary="Inspected both criteria and the principle")
        is_error, pending = self.call("submit_review", task="write-readme", report=report)
        self.assertTrue(is_error)  # and so is one with every row still pending: nothing is validated until each is filled
        self.assertIn("Pending", pending)
        self.assertEqual(self.ok("get_task", task="write-readme")["status"], "post_validation")

        for row in report["checks"] + report["principles"]:
            row.update(result="pass", method="Read notes.md", observations="It matches what the task asked for", evidence=["notes.md"])
        is_error, refused = self.call("submit_review", task="write-readme", report=dict(report, reviewer="someone-else"))
        self.assertTrue(is_error)  # the agent that did the work cannot pass it, not even by typing another name into the report
        self.assertIn("worked on this task", refused)
        self.assertEqual(self.ok("get_task", task="write-readme")["status"], "post_validation")
        worker, self.server = self.server, mcp.Server(Board(actor="reviewer", url=self.url, leases=self.root / "reviewer-leases.json"))
        done = self.ok("submit_review", task="write-readme", report=report)  # a second agent, with its own actor, passes it
        self.server = worker
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.ok("list_tasks", status="done")["count"], 1)
        self.assertEqual(self.ok("move_task", task="cut-release", status="ready")["task"]["status"], "ready")  # its dependency is done

        self.assertEqual(json.loads(self.leases.read_text()), {}, "the lease is dropped once the card leaves doing")
        self.assertNotIn(token, "\n".join(self.seen), "the lease token reached the model")

    def test_a_board_is_created_from_what_the_user_stated(self):
        stated = [{"title": "No dead links", "content": "Every link on the site resolves."}]
        made = self.ok("create_board", key="web-app", title="Web app", principles=stated)
        self.assertEqual(made["principles"], ["Report as you work", "No dead links"])  # the default first, then the user's
        self.assertEqual(self.ok("create_board", key="web-app", title="Web app", principles=stated)["id"], made["id"])  # again: no change
        softer = [{"title": "No dead links", "content": "Links should mostly work."}]
        is_error, _ = self.call("create_board", key="web-app", title="Web app", principles=softer)
        self.assertTrue(is_error)  # an agent cannot swap a board's principles by creating it again
        self.assertEqual(self.ok("create_board", key="bare", title="Bare", report_as_you_work=False)["principles"], [])
        is_error, message = self.call("create_board", key="x", title="X", report_as_you_work="yes")
        self.assertEqual((is_error, "true or false" in message), (True, True))
        self.ok("create_board", key="two", title="Two", report_as_you_work=False,
                principles=[{"title": "Be kind", "content": "One."}, {"title": "Be kind", "content": "Two."}])
        stored = next(b for b in Board(actor="lead", url=self.url, leases=self.root / "lead.json").boards() if b["source_key"] == "two")
        self.assertEqual([p["key"] for p in stored["principles"]], ["be-kind", "be-kind-2"])

    def test_the_readme_board_set_up_from_one_message(self):
        """The calls an agent makes for the README's "complete board" example, and what they must produce."""
        task = lambda key, title, priority=2, deps=(): {"key": key, "title": title, "outcome": f"{title} is done and checkable.",
                                                         "acceptance": [f"{title}: observed working"], "priority": priority,
                                                         "status": "backlog" if deps else "ready", **({"depends_on": list(deps)} if deps else {})}
        board = self.ok("create_board", key="mobile-launch", title="Mobile app launch", principles=[
            {"title": "Feature flags", "content": "Every change ships behind a feature flag."},
            {"title": "Crash-free beta", "content": "Nothing is released while the beta crash-free rate is under 99.5%."},
            {"title": "Screenshots", "content": "Every UI change comes with before and after screenshots."}])
        self.assertEqual(board["principles"], ["Report as you work", "Feature flags", "Crash-free beta", "Screenshots"])
        beta = {"source_key": "beta", "title": "Beta", "sources": [], "tasks": [
            task("testflight", "TestFlight build"), task("crash-reporting", "Crash reporting", priority=0),
            task("invite", "Invite 50 testers", deps=["testflight"]), task("feedback", "Collect feedback", deps=["invite"])]}
        store = {"source_key": "store-launch", "title": "Store launch", "sources": [], "tasks": [
            task("listing", "Store listing"), task("privacy", "Privacy labels"),
            task("release", "Release", deps=["listing", "privacy"]), task("announce", "Announce", deps=["release"])]}
        self.ok("import_plan", board="mobile-launch", plan=beta)
        self.ok("import_plan", board="mobile-launch", plan=store)
        cards = {t["key"]: t for t in self.ok("list_tasks", board="mobile-launch")["tasks"]}
        self.assertEqual(len(cards), 8)
        self.assertEqual(cards["crash-reporting"]["priority"], 0)
        is_error, _ = self.call("move_task", task="release", status="ready")
        self.assertTrue(is_error)  # the release waits for the listing and the privacy labels
        # the agent that set up the board wrote every card's criteria, so working one of them is flagged for the reviewer
        self.assertEqual(self.ok("get_task", task="testflight")["criteria_by"], "claude")
        digest = self.ok("get_context", task="testflight")["principles_sha256"]
        self.ok("claim_task", task="testflight", principles_sha256=digest)
        own = [f for f in self.ok("audit_board", board="mobile-launch")["findings"] if f["kind"] == "criteria_by_worker"]
        self.assertEqual(own, [{"kind": "criteria_by_worker", "task": cards["testflight"]["id"], "actor": "claude"}])

    def test_a_lease_token_is_stored_for_the_agent_and_never_shown_to_the_model(self):
        self.plan_a_board()
        self.ok("claim_task", task="write-readme")
        stored = json.loads(self.leases.read_text())
        (token,) = stored.values()
        self.assertEqual(stat.S_IMODE(os.stat(self.leases).st_mode), 0o600)
        self.assertNotIn(token, "\n".join(self.seen))
        self.assertNotIn(token, json.dumps(self.ok("get_task", task="write-readme")))
        self.ok("release_task", task="write-readme", note="Stepping away")
        self.assertEqual(json.loads(self.leases.read_text()), {})
        self.assertEqual(self.ok("get_task", task="write-readme")["status"], "ready")

    def test_a_failing_review_sends_the_card_back_with_what_to_fix(self):
        self.plan_a_board()
        self.ok("claim_task", task="write-readme")
        for i in (0, 1):
            self.ok("record_evidence", task="write-readme", criterion=i, ref="notes.md", note="Observed")
        self.ok("check_criteria", task="write-readme")
        self.ok("move_task", task="write-readme", status="post_validation")
        report = self.ok("prepare_review", task="write-readme")
        report.update(reviewer="claude", summary="Looked, and one criterion does not hold")
        for row in report["checks"]:
            row.update(result="pass", method="Read", observations="Fine", evidence=["notes.md"])
        report["checks"][1].update(result="fail", expected="Every command ran", avoid="Claiming it did", fix="Run the remaining commands")
        outcome = self.ok("submit_review", task="write-readme", report=report)
        self.assertEqual(outcome["status"], "review")
        self.assertIn("repair", outcome["next"])
        self.assertEqual(len(self.ok("get_task", task="write-readme")["corrections"]), 1)

    def test_planning_is_idempotent_and_refuses_a_changed_plan(self):
        first = self.plan_a_board()
        self.assertTrue(first["imported"])
        self.assertEqual([t["key"] for t in first["tasks"]], ["write-readme", "cut-release", "announce"])
        self.assertFalse(self.ok("import_plan", board="demo", plan=PLAN)["imported"])
        is_error, why = self.call("import_plan", board="demo", plan=dict(PLAN, title="Changed behind your back"))
        self.assertTrue(is_error)
        self.assertIn("different import content", why)
        is_error, why = self.call("import_plan", board="nope", plan=PLAN)
        self.assertTrue(is_error)
        self.assertIn("demo", why)  # it lists the boards that do exist

    def test_audit_takes_a_board_key_or_id_and_names_the_next_task(self):
        self.plan_a_board()
        board_id = self.ok("list_boards")[0]["id"]
        for ref in ("demo", board_id):
            self.assertEqual(self.ok("audit_board", board=ref)["next_task"], self.ok("get_task", task="write-readme")["id"])

    def test_a_server_that_is_not_running_is_explained(self):
        offline = mcp.Server(Board(actor="claude", url="http://127.0.0.1:1", leases=self.root / "x.json"))
        result = offline.handle(rpc("tools/call", {"name": "list_boards", "arguments": {}}))["result"]
        self.assertTrue(result["isError"])
        self.assertIn("python -m agilevow serve", result["content"][0]["text"])


class StdioTests(unittest.TestCase):
    """A real child process, to prove the framing and that stdout carries nothing but protocol."""

    def test_a_session_over_real_pipes(self):
        with tempfile.TemporaryDirectory() as folder:
            home, root = Path(folder, "home"), Path(folder, "project")
            home.mkdir(), root.mkdir()
            http = make_server(root, port=0, quiet=True)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            self.addCleanup(http.server_close)
            self.addCleanup(http.shutdown)
            url = f"http://127.0.0.1:{http.server_port}"
            lead = Board(actor="lead", url=url, leases=Path(folder, "lead.json"))
            lead.import_plan(lead.create_board("demo", "Demo"), PLAN)

            lines = [rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}, 1),
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     rpc("tools/list", mid=2),
                     rpc("tools/call", {"name": "claim_task", "arguments": {"task": "write-readme"}}, 3),
                     rpc("tools/call", {"name": "list_boards", "arguments": {}}, 4)]
            # The repo may hold its own board's data folder (a developer can run AgileVow on this repo); the session must
            # not add anything to it, whatever was there before.
            files_in_repo_data = lambda: sorted(str(p) for p in REPO.glob(".agilevow/**/*"))
            before = files_in_repo_data()
            env = dict(os.environ, HOME=str(home), AGILEVOW_URL=url, AGILEVOW_ACTOR="agent x/1")
            env.pop("AGILEVOW_LEASES", None)
            done = subprocess.run([sys.executable, "-m", "agilevow", "mcp"], input="\n".join(json.dumps(m) for m in lines) + "\n",
                                  capture_output=True, text=True, cwd=REPO, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            replies = [json.loads(line) for line in done.stdout.splitlines()]  # every stdout line is JSON, or this raises
            self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
            self.assertEqual(len(replies[1]["result"]["tools"]), 16)
            self.assertFalse(replies[2]["result"]["isError"], replies[2])
            self.assertIn("ready; board server", done.stderr)
            # An MCP client chooses the working directory, so the lease file lives under the home folder, one per actor.
            leases = home / ".agilevow" / "leases-agent_x_1.json"
            self.assertTrue(leases.is_file(), list(home.rglob("*")))
            self.assertEqual(stat.S_IMODE(os.stat(leases).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(leases.parent).st_mode), 0o700)
            self.assertEqual(files_in_repo_data(), before, "the MCP session wrote into the folder it was started in")


class StreamHygieneTests(unittest.TestCase):
    def test_a_stray_print_cannot_corrupt_the_protocol_stream(self):
        out, err = io.StringIO(), io.StringIO()
        real = sys.stdout, sys.stderr, sys.stdin
        original = mcp.Server.handle

        def noisy(server, message):
            print("stray debugging output")  # stdout belongs to the protocol; this must land on stderr
            return original(server, message)

        sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO(json.dumps(rpc("ping")) + "\n")
        mcp.Server.handle = noisy
        try:
            with tempfile.TemporaryDirectory() as folder:
                code = mcp.main(["--leases", str(Path(folder) / "l.json"), "--url", "http://127.0.0.1:1"])
        finally:
            mcp.Server.handle = original
            sys.stdout, sys.stderr, sys.stdin = real
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line) for line in out.getvalue().splitlines()], [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        self.assertIn("stray debugging output", err.getvalue())


if __name__ == "__main__":
    unittest.main()
