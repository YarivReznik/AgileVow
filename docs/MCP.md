# AgileVow over MCP

`python -m agilevow mcp` serves the worker loop to an agent as [Model Context Protocol](https://modelcontextprotocol.io) tools, over stdio. Anything that can launch an MCP server (Claude Code, Claude Desktop, Cursor, your own client) can then resume work, read the board's principles, claim a card, record evidence and validate, with no shell and no `curl`.

It is a thin client of a running `agilevow serve`. Every tool goes through the same HTTP API, and so the same checks, as the command line and the web board. There is no second way to change a card, and what an agent does shows up on the board page as it happens.

```text
agent  --stdio-->  agilevow mcp  --HTTP, 127.0.0.1-->  agilevow serve  -->  .agilevow/agilevow.sqlite3
```

## Set it up

Start the board first (see the [README](../README.md)), then register the MCP server with your client. Install the package so the `agilevow` command exists (`pip install .` from a clone, or `pipx install .`).

**Claude Code**

```sh
claude mcp add agilevow -e AGILEVOW_ACTOR=claude -- agilevow mcp
```

Without installing, from a clone:

```sh
claude mcp add agilevow -e AGILEVOW_ACTOR=claude -e PYTHONPATH=/path/to/agilevow -- python3 -m agilevow mcp
```

**Claude Desktop and clients that read a JSON config** (`claude_desktop_config.json` and similar):

```json
{
  "mcpServers": {
    "agilevow": {
      "command": "agilevow",
      "args": ["mcp"],
      "env": { "AGILEVOW_URL": "http://127.0.0.1:8765", "AGILEVOW_ACTOR": "claude" }
    }
  }
}
```

| Setting | Flag | Environment | Default |
| --- | --- | --- | --- |
| Where `agilevow serve` listens | `--url` | `AGILEVOW_URL` | `http://127.0.0.1:8765` |
| Who the agent acts as | `--actor` | `AGILEVOW_ACTOR` | `agent` |
| Lease token file | `--leases` | `AGILEVOW_LEASES` | `~/.agilevow/leases-ACTOR.json` |

Give each agent its own actor name. The actor owns the claims it makes, and the lease file is keyed by it.

The lease file defaults to your home folder, not the working directory, because an MCP client chooses the directory it starts the server in. The folder is created `0700` and the file `0600`.

If the board server is not running, a tool call returns an error that says so (`Is python -m agilevow serve running?`); nothing else fails.

## The tools

Read-only tools are marked. None deletes anything: a card can be cancelled or reopened, never removed.

| Tool | Read-only | What it does |
| --- | --- | --- |
| `list_boards` | yes | Boards on the server: key, id, title, and how many binding principles each has. |
| `list_tasks` | yes | Tasks, optionally for one board and one status. Start here to resume work. |
| `get_task` | yes | One task in full: outcome, criteria, evidence, checklist, corrections, links, owner, status, recent comments and events. |
| `get_context` | yes | The board's binding principles and the task context, with `principles_sha256`. Read it before claiming. |
| `claim_task` | | Claim a ready task. A board with principles needs the `principles_sha256` from `get_context`. |
| `renew_lease` | | Extend your lease (60 to 3600 seconds). |
| `release_task` | | Give a task back, with a handoff note that becomes its next step. |
| `record_evidence` | | Evidence for one criterion (numbered from 0): a reference and a note. Merges with what is there. |
| `check_criteria` | | Tick criteria that have evidence: those listed, or all. Earlier ticks are kept. |
| `move_task` | | Move to `review`, `post_validation`, `blocked`, `cancelled`, `ready` or `backlog`. |
| `add_comment` | | A note, a conversation excerpt, or a decision. |
| `audit_board` | yes | What to work on next and what is wrong: queues, stale or expired claims, missing evidence. |
| `prepare_review` | yes | For a task in `post_validation`: a report with one pending row per criterion and per principle. |
| `submit_review` | | The filled report. All pass: Done. Any fail: back to `review` with a repair list. |
| `import_plan` | | Plan work on an existing board from a plan like `examples/plan.json`. Repeating it changes nothing. |

A task argument is a key (`write-readme`) or an id (`task-...`); add `plan` only when the same key exists in two plans.

## How an agent uses it

The server sends these rules to the client when the session opens, in its `instructions`:

1. Resume with `list_tasks` and `audit_board`. Continue a card you already hold before starting another.
2. `get_context`, read the principles, then `claim_task` with the hash.
3. Work. `record_evidence` for every criterion with real results only, then `check_criteria`.
4. `move_task` to `review` or `post_validation`. There is no move to Done.
5. Done is reached only by `prepare_review`, inspecting the work and filling every row honestly, then `submit_review`, by someone other than the worker: the agent may fail its own card, but a pass must come from a second agent or a person.
6. `release_task` with a note before switching to other work.

## What is deliberately missing

- **No tool creates a board.** A board's binding principles are the user's requirements. An agent can read them and plan inside the board (`import_plan`) but does not get to invent them. Create the board yourself (`agilevow client create-board`, or the web page).
- **No way to Done except `submit_review`.** `move_task` does not accept `done` or `doing`; its schema rejects them before anything reaches the server, and the server would refuse them anyway.
- **No lease token is ever returned.** The model sees that it holds a card, not the secret that proves it. The token stays in the owner-only lease file.
- **Bad arguments are answered, not raised.** A missing field or a wrong type comes back as a tool result with `isError: true` and a sentence saying what is wrong, so the model can correct itself. Only an unknown tool name or a malformed message is a protocol error.

## Limits

- **The worker cannot pass its own card.** `submit_review` validates as the actor this server runs as (the report's `reviewer` is replaced by it), and the board refuses a passing validation from anyone who claimed the card or recorded its evidence, ticks or corrections. The agent may submit a failing review of its own card. To reach Done, register a second server with its own actor and let that agent (or you, in the browser or with `review submit`) review it:

  ```sh
  claude mcp add agilevow-review -e AGILEVOW_ACTOR=reviewer -- agilevow mcp
  ```

  Actor names are not authenticated, so this stops the default path of an agent grading itself, not one configured to pose as someone else. See [SECURITY.md](../SECURITY.md).
- **Tools only.** No MCP resources or prompts, no sampling, no logging capability. Tool output over 100,000 characters is truncated with a note.
- **Protocol versions** `2025-06-18`, `2025-03-26` and `2024-11-05` are accepted; the client's version is echoed when it is one of these, otherwise the newest is offered. Messages are one JSON object per line, and batches are accepted.
- **Not yet run against an official MCP client SDK or a shipping client.** The test suite drives the server with a hand-written client over real pipes, and checks the framing, the handshake, the schemas and the whole claim-to-Done loop. If a client rejects it, that is a bug to report.
- **stdout belongs to the protocol.** Anything the process would print is redirected to stderr, where the server also logs.

`tests/test_mcp.py` covers the handshake, the schemas and refusals, lease handling, the stream rules, output truncation and the whole claim-to-Done loop. It does not cover the client-side setup commands above.
