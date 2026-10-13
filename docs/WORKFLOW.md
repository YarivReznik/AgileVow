# The worker loop

How an agent or a person works a card on AgileVow, from picking it up to Done. The rules the server enforces are in [API.md](API.md); this page is the procedure that stays inside them.

Every command runs as `python3 -m agilevow ...` (or `agilevow ...` after `pip install .`). Set the server and who you are once:

```sh
export AGILEVOW_URL=http://127.0.0.1:8765   # the default
export AGILEVOW_ACTOR=alice                 # your name; use a distinct one per agent
```

`client` keeps your lease tokens in `./.agilevow/leases.json`, keyed by task id only. Give each actor its own file (`AGILEVOW_LEASES=/path/to/alice-leases.json`) or run from the project folder, and do not share a file between actors: a token belonging to someone else is sent with your name and the server answers `409 claim lease or owner does not match`.

## The loop

**1. Resume before you pick anything new.** List what exists and ask the manager what is wrong and what is next. The manager takes the board **id**; `client` takes the board **key**.

```sh
python3 -m agilevow client boards
python3 -m agilevow client tasks --board demo
python3 -m agilevow manager --board demo
```

If you already hold a `doing` card, continue it. `client tasks` does not show owners; the board page does, as does `GET /api/tasks/item?id=TASK-ID`. Finish, checkpoint or release a card before claiming another, and never take over someone else's lease.

**2. Read the card, principles first.**

```sh
python3 -m agilevow client context write-readme
```

The output is the board's binding principles, then the task, acceptance criteria, sources and next step. The last line is `principles_sha256`. If you pass this text to another worker, pass all of it.

**3. Claim with the hash.**

```sh
python3 -m agilevow client claim write-readme --ack-principles HASH --seconds 1800
```

A board with principles refuses a claim without the current hash (`409`). Boards without principles need no `--ack-principles`. A claim also fails if a dependency is not done or a source file changed.

**4. Keep the lease alive.** The server default is 900 s; the CLI asks for 3600 s unless you pass `--seconds` (60 to 3600). Renew during long work; `--seconds` sets the new length.

```sh
python3 -m agilevow client renew write-readme --seconds 1800
```

If the lease expires the card stays `doing` but you can no longer write to it. See "When a lease runs out" below.

**5. Record evidence as you go, one entry per acceptance criterion.** Criteria are numbered from 0. A reference is a file path relative to the project root, a URL, or any identifier; the note says what it shows.

```sh
python3 -m agilevow client evidence write-readme 0 notes.md --note "Followed the steps on a clean checkout"
python3 -m agilevow client comment write-readme "Kept the install steps to two commands" --kind decision
```

Record decisions and user corrections as comments (`note`, `conversation` or `decision`), with the real source in `source_ref` when there is one. Do not invent a citation. Update `next_step` so another session can continue without reconstructing yours.

**6. Tick the checklist when a criterion is verified.** A tick requires evidence for that criterion with a reference and a note. `client check` ticks every criterion that has evidence, or only the indexes you list, and keeps the ticks already there (the API replaces the whole list; the command does the merge for you):

```sh
python3 -m agilevow client check write-readme        # all of them
python3 -m agilevow client check write-readme 0      # only criterion 0
```

**7. Move to Review, then Post validation.** Review is an optional checkpoint that releases your lease. Post validation needs evidence and a tick for every criterion, unchanged sources, and finished dependencies.

```sh
python3 -m agilevow client move write-readme review
python3 -m agilevow client move write-readme post_validation
```

`move ... done` is always refused: no transition reaches Done.

**8. Validate.** The report has one row per criterion and per principle. Prepare it, inspect the work, fill every row with what you actually observed, submit.

```sh
python3 -m agilevow review prepare --task TASK-ID --report /tmp/report.json
python3 -m agilevow review submit  --task TASK-ID --report /tmp/report.json
```

Run both as the reviewer (`AGILEVOW_ACTOR`, or `--actor`). `prepare` writes a pending report signed with that name and will not overwrite an existing file; `submit` refuses a report signed with any other name, so a review cannot be passed off as someone else's. Fill in `summary`, then for every row in `checks` and `principles`:

```json
{"criterion": 0, "acceptance": "The README has install and run steps that work on a clean machine.",
 "result": "pass", "method": "Read notes.md", "observations": "Matches the criterion",
 "evidence": ["notes.md"], "expected": "", "avoid": "", "fix": ""}
```

`result` is `pass` or `fail`. `method`, `observations` and at least one `evidence` reference are required on every row, failures included. A `fail` also needs `expected` (a correct example), `avoid` (an unwanted example) and `fix` (what to change). A principle that does not apply to this task still needs a row: say why it is unaffected and cite something inspectable; do not claim unrelated work is done.

`submit` refuses a pending row, a report prepared against an older revision of the card, principles that changed since `prepare`, changed sources, and a report over about 11,000 characters. Otherwise it sends the `validate` action and then stores the whole report as a `decision` comment on the card, so a refused report leaves nothing behind. The first evidence reference of each row goes into the validation record; the comment keeps them all. Its errors surface as Python tracebacks.

The reviewer must be someone who did not work on the card. A passing report from its owner, or from anyone who claimed it or recorded its evidence, ticks or corrections, is refused (`403`). The worker can still submit a failing report against its own card. Use a second agent with its own actor name, or a person.

**9. After a pass the card is Done.** Run the manager again; any card that was waiting on this one can now become Ready (`client move OTHER ready`).

## When validation fails

All rows are recorded. The card goes back to `review` with a `corrections` list: each failed row, unverified, with its `observed`, `expected`, `avoid` and `fix`; the failed criteria are unticked, and `next_step` says to claim the repair. Then:

1. `client context` again (the principles may have changed), then `client claim TASK --ack-principles HASH`. A card in Review can be claimed only while it has an unverified correction.
2. Do the repair. For every correction, record a check with the note and evidence that show it is fixed, and re-tick the criteria that failed:

```sh
python3 - <<'EOF'
from agilevow.client import Board
b = Board()
t = b.task("write-readme")
fixes = [{"id": c["id"], "checked": True, "note": "Ran the remaining commands and pasted their output", "ref": "notes.md"}
         for c in t["corrections"] if not c["checked"]]
b.update(t, correction_checks=fixes)
EOF
python3 -m agilevow client check write-readme
```

3. `client move TASK post_validation`, then `review prepare` with a new report file and `review submit` again. Both sets are checked again from scratch; only a run with no failures reaches Done.

## When a lease runs out

`doing` does not change on expiry. Look at the last events, the comments and `next_step`, and at the work itself; an expired lease does not prove the worker stopped. Then recover with a note that says where to resume:

```sh
python3 - <<'EOF'
from agilevow.client import Board
b = Board()
b.action(b.task("write-readme"), "recover", note="Previous session ended; resume from the last comment")
EOF
```

The card returns to `ready` with no owner and the old token is dead. Claim it as normal. An unexpired lease cannot be recovered; wait or ask its holder.

To stop deliberately, leave a safe checkpoint and release with a handoff note. The note is required:

```sh
python3 -m agilevow client release write-readme --note "Pausing: criterion 1 not run yet; notes.md has the first result"
```

## Blocking, scope changes and drift

- **Blocked.** `client move TASK blocked --note "what you are waiting for"`. No lease is needed and any claim is revoked. `client move TASK ready` unblocks it.
- **A source file changed.** The card shows `source_changed`, cannot be claimed or completed, and the manager reports `sources_changed`. Read the new file and the card, then reconcile with a note (Python client: `b.action(task, "reconcile", note="...")`, or the API). Reconciling clears evidence and ticks and sends started work back to `backlog`.
- **The user changes the scope.** Edit the outcome, criteria or dependencies with a note. That resets the card the same way. Do not edit source files to make old progress look right.
- **Reopening a Done card** is a move to `backlog` with a note.

## The manager

`manager --board BOARD` (a board key or its id) reads the board and prints JSON. It changes nothing.

| Field | Meaning |
| --- | --- |
| `doing` | Ids of cards in `doing`. |
| `post_validation_queue` | Cards waiting for `validate`, by priority then oldest. |
| `correction_queue` | Cards in `review` with an unverified correction (repairs). |
| `review_queue` | Every card in `review`, so a repair appears here too. |
| `ready_queue` | `ready` cards with finished dependencies and unchanged sources, by priority (lower first) then oldest. |
| `next_task` | The first of post-validation, then repairs, then ready; `null` when the number of `doing` cards has reached the WIP limit, or nothing is eligible. |
| `findings` | Problems, below. |
| `wip_limit`, `mode`, `worker_policy` | `--wip-limit` (default 1), always `advisory`, a reminder of the procedure. |

| Finding | Raised when |
| --- | --- |
| `wip_exceeded` | More cards are `doing` than the limit. The server does not enforce this; two claims will both succeed. |
| `lease_expired` | A `doing` card is past `claim_expires_at`. |
| `stale_activity` | A `doing` card has not changed for `--stale-seconds` (default 3600). |
| `post_validation_required` | A card is in Post validation. |
| `principles_read_required` | A card needs a principles acknowledgement: the principles changed since, or nobody has acknowledged them yet (true of every unclaimed card on a board with principles). |
| `sources_changed` | A card that is not in backlog or cancelled has a changed source file, Done cards included. |
| `dependencies_unfinished` | A `ready`, `doing`, `review` or `post_validation` card depends on one that is not done. |
| `missing_evidence` | A card in `review`, `post_validation` or `done` has a criterion with no evidence (`criteria` lists the indexes). |
| `blocked` | A card is `blocked` (`reason` carries the text). |

Run it before choosing work and after finishing, blocking or checkpointing. If the audit fails, do not fall back to claiming at random.

It deliberately does not start agents, perform the validation, schedule anything, recover leases, promote backlog or change a card. Moving a card to Ready is a human or agent decision made with `client move`; the manager only ranks what is already Ready.
