<!-- Paste everything below into your AGENTS.md or CLAUDE.md. Replace BOARD-KEY and the URL. -->
## Task board (AgileVow)

The AgileVow board is the record of what work exists and what state it is in. The server is at `http://127.0.0.1:8765`; set `AGILEVOW_URL` if it is elsewhere and `AGILEVOW_ACTOR` to your own name. Run `python3 -m agilevow ...` (or `agilevow ...` if installed). `TASK` is a task key or id. If your client has the AgileVow MCP server (see docs/MCP.md), use its tools instead of the commands below (`list_tasks`, `get_context`, `claim_task`, `record_evidence`, `check_criteria`, `move_task`, `prepare_review`, `submit_review`, `release_task`); the rules are identical.

Before you work:
- Resume first. Run `python3 -m agilevow client tasks --board BOARD-KEY` and `python3 -m agilevow manager --board BOARD-KEY`. Continue a card you already hold before starting another. Do not start work that has no card.
- Read before claiming: `python3 -m agilevow client context TASK`. It prints the binding principles, then the task. They are the user's requirements; a newer instruction from the user outranks them.
- Claim with the hash on the last line: `python3 -m agilevow client claim TASK --ack-principles HASH`. Never take a card someone else holds. If a lease has expired, read the card's history first, then recover it with a note.

While you work:
- Renew the lease on long work: `python3 -m agilevow client renew TASK`. After expiry you can no longer write to the card.
- Record evidence for every acceptance criterion, numbered from 0: `python3 -m agilevow client evidence TASK N REF --note "what this shows"`. Use real results only (command output, a file you wrote, a link). Never invent or reconstruct evidence.
- Record decisions and user corrections as comments: `python3 -m agilevow client comment TASK "text" --kind decision`.
- If the scope or a source document changes, stop and record it. Do not edit documents so that old work looks correct.

To finish:
- Tick each criterion once its evidence is recorded: `python3 -m agilevow client check TASK` (all criteria that have evidence) or `... check TASK N` (just criterion N). A tick is refused without evidence.
- Move to validation: `python3 -m agilevow client move TASK post_validation`.
- Never mark a card Done. There is no such move and no workaround. A card is Done only when `python3 -m agilevow review submit --task TASK-ID --report FILE` passes every criterion and every principle (`TASK-ID` is the `task-...` id that `client tasks` prints).
- Validate honestly: `review prepare`, inspect the work, fill every row with what you observed, then `review submit`. On a fail the card returns to Review with expected, avoid and fix. Claim the repair, fix it, and validate again.
- You cannot pass a card you worked on (claimed, or recorded its evidence, ticks or corrections): the board refuses it. You may fail your own card. For a pass, hand the card to a reviewer who did not work on it: a person, or a second agent running under its own `AGILEVOW_ACTOR`. `review submit` signs the report with the actor running it and refuses any other name.

Before you switch to other work:
- Release the card with a handoff note, which becomes its next step: `python3 -m agilevow client release TASK --note "HANDOFF"`.
