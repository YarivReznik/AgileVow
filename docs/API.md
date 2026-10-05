# AgileVow API reference

The server speaks JSON over HTTP on `http://127.0.0.1:8765` (change it with `serve --port`). This page is derived from `agilevow/board.py` and `agilevow/api.py`; the examples were captured from a running server, with ids shortened (`task-<id>`) and long text elided (`...`).

- [Concepts](#concepts) | [Conventions](#conventions) | [Lifecycle](#lifecycle) | [Routes](#routes) | [Objects](#objects)
- [Boards and principles](#boards-and-principles) | [Plans, sources and import](#plans-sources-and-import) | [Tasks](#tasks)
- [Actions](#actions) | [Claims and leases](#claims-and-leases) | [Evidence and the checklist](#evidence-and-the-checklist)
- [Post validation](#post-validation) | [Comments, attachments and artifacts](#comments-attachments-and-artifacts) | [Export](#export)
- [Errors](#errors) | [Limits](#limits)

## Concepts

A **board** groups related work and holds the **binding principles**. A **plan** (the web page calls it a "mini project") is a small project inside a board. A **task** (a card) is one outcome with acceptance criteria.

| Record | id | key | key is unique |
| --- | --- | --- | --- |
| board | `board-<32 hex>` | `source_key` | in the database |
| plan | `plan-<32 hex>` | `source_key` | within its board |
| task | `task-<32 hex>` | `key` | within its plan |

A key is 1 to 160 characters matching `[A-Za-z0-9][A-Za-z0-9._:/-]*`. The key is yours and stable; the id is assigned by the server. Dependencies and `task` links use ids, except in a plan import, where `depends_on` uses local keys.

## Conventions

- Every JSON `POST` body is an object of at most 1 MB (an upload is raw bytes; see [attachments](#comments-attachments-and-artifacts)). Unknown fields are rejected (`unknown fields: wat`). NaN and infinity are rejected.
- Every mutation carries `actor`: 1 to 128 characters of text you assert. Nothing authenticates it.
- Updates and actions carry the record's current `revision` (an integer). A mismatch is `409 stale revision; reload the current item`. Every successful write adds one to the record's revision. Re-read, check that your change still makes sense, then retry; do not resend an old request with a new revision.
- Text is stripped of surrounding whitespace. Text longer than its limit, or containing a NUL, is rejected, not truncated. See [Limits](#limits).
- Timestamps (`created_at`, `updated_at`, `at`, `claim_expires_at`) are Unix seconds as floats.
- The HTTP layer adds `href` to links, sources, evidence and attachments. These are for navigation; do not store them. Replies to `plans/import` are not enriched, so their tasks carry no `href`.
- Replies are `Content-Type: application/json` with `Cache-Control: no-store`. There are no CORS headers.

## Lifecycle

| Status | Meaning |
| --- | --- |
| `backlog` | Scoped but not available to start. |
| `ready` | Dependencies are done; can be claimed. |
| `doing` | Claimed. The holder of the lease may change the card. |
| `review` | A checkpoint, or repair work after a failed validation. |
| `post_validation` | Execution finished; waiting for `validate`. |
| `done` | A `validate` action passed every criterion and every principle. |
| `blocked` | Waiting on something recorded in `blocked_reason`. |
| `cancelled` | No longer pursued. History is kept. |

`transition` (see [Actions](#actions)) allows exactly these moves, taken from `TRANSITIONS`:

| From | To |
| --- | --- |
| `backlog` | `ready`, `blocked`, `cancelled` |
| `ready` | `backlog`, `blocked`, `cancelled` |
| `doing` | `review`, `post_validation`, `blocked`, `cancelled` |
| `review` | `post_validation`, `ready`, `blocked`, `cancelled` |
| `post_validation` | `blocked`, `cancelled` |
| `done` | `backlog` |
| `blocked` | `ready`, `backlog`, `cancelled` |
| `cancelled` | `backlog` |

**No transition targets `done`, and none targets `doing`.** Those two are reached only by other actions:

- `claim` moves `ready` (or `review`, when a failed validation left unverified corrections) to `doing`.
- `validate` moves `post_validation` to `done` when every check passes, or to `review` when any fails.

Guards on a transition:

| Move | Requires |
| --- | --- |
| to `blocked` or `cancelled`, or out of `done` or `cancelled` | a non-empty `note` (`400` otherwise). |
| out of `doing` to `review` or `post_validation` | the owner's valid `lease`; the move releases the claim. Blocking and cancelling need none, and revoke the claim. |
| to `ready` | every task in `depends_on` is `done` (409). |
| to `review` or `post_validation`, on a board with principles | the caller has acknowledged the current principles (see [Boards and principles](#boards-and-principles)); otherwise send `principles_sha256` (409). |
| to `post_validation` | every criterion has evidence, every criterion is ticked, every correction is verified, and no source file changed (409). |
| out of `done` | clears the task's evidence. Ticks and `owner` are left as they were. |

## Routes

| Method | Route | Fields | Purpose |
| --- | --- | --- | --- |
| GET | `/`, `/board.css`, `/board.mjs`, `/i18n.mjs` | | The board page and its assets. Nothing else is served as a file. |
| GET | `/api/boards` | | `{boards}`. |
| POST | `/api/boards/create` | `actor`, `source_key`, `title`; optional `description`, `scope`, `principles` | Create a board. Same content again returns the existing board with `created: false`; different content under the same key is 409. |
| POST | `/api/boards/update` | `actor`, `id`, `revision`; optional `title`, `description`, `scope`, `principles`, `note` | Edit a board. Changing `principles` needs a `note`. |
| GET | `/api/tasks` | optional query `board`, `plan` (ids) | The snapshot: `{version, link_kinds, boards, plans, tasks}`. |
| GET | `/api/tasks/item` | query `id` | `{task, events}` for one task. |
| GET | `/api/tasks/export` | | The audit archive. |
| POST | `/api/plans/create` | `actor`, `board_id`, `source_key`, `title`; optional `description`, `scope`, `kind`, `sources` | Create a plan. |
| POST | `/api/plans/update` | `actor`, `id`, `revision`; optional `title`, `description`, `scope`, `kind`, `sources`, `note` | Edit a plan. Changing `sources` needs a `note` and reopens its tasks. |
| POST | `/api/plans/import` | as `plans/create`, plus `tasks` (1 to 1000) | Create a plan and its tasks in one transaction. Idempotent. |
| POST | `/api/tasks/create` | `actor`, `plan_id`, `key`, `title`, `outcome`, `acceptance`; optional `depends_on`, `priority`, `status`, `owner`, `next_step`, `links`, `evidence`, `sources`, `tags` | Add one task. |
| POST | `/api/tasks/update` | `actor`, `id`, `revision`; optional `lease`, `note`, and any editable task field | Edit a task. Never changes `status`. |
| POST | `/api/tasks/action` | `actor`, `id`, `revision`, `op`; see [Actions](#actions) | Claim, renew, release, recover, transition, reconcile, acknowledge, validate. |
| POST | `/api/tasks/comment` | `actor`, `id`, `revision`, `body`; optional `kind`, `source_ref`, `lease` | Append a comment. |
| POST | `/api/tasks/attach?id=TASK` | raw bytes, `X-Task-*` headers | Upload an attachment. |
| GET | `/api/tasks/attachment` | query `task`, `attachment` | Download an attachment. |
| GET | `/api/tasks/artifact` | query `task`, `path` | Read a project file that is named on that task. |

Any other `GET` under `/api/tasks` or `/api/boards`, and any other `POST`, is `404`. Other HTTP methods get the standard library's `501` page.

```sh
curl -s http://127.0.0.1:8765/api/boards
curl -s -H 'Content-Type: application/json' -d '{"actor":"alice","source_key":"demo","title":"Demo"}' \
  http://127.0.0.1:8765/api/boards/create
```

## Objects

**Board**

```json
{"id": "board-<id>", "revision": 1, "source_key": "api-demo", "title": "API demo",
 "description": "Captured for the reference.", "scope": "demo",
 "principles": [{"key": "terse", "title": "Terse", "content": "Say it in one line."}],
 "created_at": 1791200607.73, "updated_at": 1791200607.73}
```

`principles` is absent when a board was created without it.

**Plan**

```json
{"id": "plan-<id>", "revision": 1, "board_id": "board-<id>", "source_key": "calc",
 "title": "Calculator", "description": "A tiny plan.", "scope": "demo", "kind": "code",
 "sources": [{"path": "docs/brief.md", "sha256": "833af69f..."}],
 "created_at": 1791200617.28, "updated_at": 1791200617.28}
```

**Task** (the same object comes back from every read and every task mutation; derived fields are computed on read):

| Field | Meaning |
| --- | --- |
| `id`, `revision`, `plan_id`, `board_id`, `key`, `title`, `outcome` | Identity and the authored text. |
| `acceptance` | The criteria, an array of strings. A criterion is referred to by its 0-based index. |
| `status`, `owner`, `claim_expires_at` | Lifecycle. `owner` is the actor who claimed it; `claim_expires_at` is null outside `doing`. |
| `depends_on` | Task ids in the same plan. |
| `priority` | 0 to 3, default 2. Lower is more urgent: the manager sorts ascending. |
| `next_step`, `blocked_reason` | Handoff text; the reason for `blocked`. |
| `links`, `tags` | See [Tasks](#tasks). |
| `sources` | `[{path, sha256, href}]`: files fingerprinted at creation. |
| `evidence` | `[{criterion, ref, note, href?}]`. |
| `checklist_checks` | Stored ticks `[{criterion, checked, actor, at}]`. Absent until first set. |
| `checklist` | Derived: one row per criterion `{criterion, text, checked, actor?, at?}`. |
| `corrections` | Present after a failed validation: `[{id, criterion or principle_key, passed, observed, ref, expected, avoid, fix, checked, verification, verification_ref, validation_id}]`, plus `checked_by` and `checked_at` once verified. |
| `validations` | The last 50 validation records, oldest first. |
| `comments`, `attachments` | Append-only lists. |
| `principles_acknowledgement` | `{sha256, board_revision, actor, at}` for the last acknowledgement. |
| `principles_changed`, `principles_need_acknowledgement` | Derived booleans; see [Principles](#boards-and-principles). |
| `changed_sources`, `source_changed` | Derived: sources whose file no longer matches `[{path, recorded_sha256, current_sha256, source_missing}]`. |
| `understanding_context` | Derived: what to read first. |
| `created_at`, `updated_at` | Timestamps. |

The lease token and its hash never appear in a task, a snapshot or an export.

`understanding_context`:

```json
{"board_id": "board-<id>", "board_revision": 2,
 "principles_sha256": "b6d20f4e615aae36255bbbaa27dfcb9a0b24ae31160152182091b0b331b72908",
 "principles": [{"key": "terse", "title": "Terse", "content": "Say it in one line, no exceptions."}],
 "read_order": ["principles", "outcome", "acceptance", "sources", "next_step"],
 "authority": "user/project requirements",
 "text": "# Binding principles\nRead these board-wide ...\n\n# Task\nAdd numbers\n\n..."}
```

`text` is the principles, then the task, acceptance, sources and next step, ready to hand to another worker. `principles_sha256` is the SHA-256 of the principles array and changes whenever any principle changes.

**Event** (`events` in `/api/tasks/item` and `/api/tasks/export`):

```json
{"id": 5, "revision": 2, "at": 1791200627.89, "actor": "alice", "op": "claim",
 "board_id": "board-<id>", "plan_id": "plan-<id>", "task_id": "task-<id>",
 "from_status": "ready", "to_status": "doing", "note": "",
 "changes": ["claim_expires_at", "owner", "principles_acknowledgement", "status"]}
```

`op` is one of `create_board`, `update_board`, `create_plan`, `update_plan`, `import_plan`, `create_task`, `import_task`, `update_task`, `correct_task`, `claim`, `renew`, `release`, `recover`, `transition`, `reconcile`, `acknowledge_principles`, `validate`, `comment`, `attach`, `plan_source_correction`. Plan and board events have `task_id: null`; board events that set principles also carry the `principles`. Event `id`s are global and increase.

**Comment**: `{id, author, kind, body, source_ref, created_at}`. **Attachment**: `{id, filename, kind, sha256, size, mime, path, created_at, author, href}`.

## Boards and principles

`principles` is an ordered array of `{key, title, content}`: at most 24, keys unique, 8000 characters of content each and 64000 characters of titles and content together. They are the board's standing requirements and are inherited by every task on the board, live: edit them once and every task's `understanding_context` changes. Changing them needs a `note`, and each version is recorded in the board's events (within the retention limit).

- **Claim** requires `principles_sha256` equal to the task's current `understanding_context.principles_sha256` whenever the board has principles (409 otherwise).
- **Transitions** to `review` or `post_validation`, and `validate`, require (on a board with principles) that the acting actor's last acknowledgement matches the current hash; if not, send `principles_sha256` with the action (409 otherwise). An acknowledgement is per actor and records who and when. It is a record that the current text was fetched, not proof that it was read.
- **Changed principles** do not alter a task's revision or status. They set `principles_changed` (the stored acknowledgement is for older text) and `principles_need_acknowledgement` (it is not for the current text; also true for a task nobody has acknowledged yet). Read the new text, then send `op: "acknowledge_principles"` with the new hash.

## Plans, sources and import

A **source** is a project file a plan or task depends on. Give a path, or `{"path": "docs/brief.md", "sha256": "..."}` to assert the content you read. The server hashes the file itself, and a wrong `sha256` is `409 source fingerprint changed`. A path must be relative, without `.`/`..` components or backslashes; the file must exist, be a regular file of at most 16 MiB, and no component may be a symlink. Sources are stored as `{path, sha256}`, sorted by path.

A task inherits its plan's sources and cannot replace their recorded fingerprints. When a file on disk stops matching, the task shows `source_changed: true` and cannot be claimed or completed. `op: "reconcile"` (with a `note`) re-hashes the task's sources and applies the [correction rules](#tasks). Changing a plan's `sources` does the same to every task in the plan, recorded as a `plan_source_correction` event.

**Import** creates a plan and all its tasks atomically:

```json
{"actor": "alice", "board_id": "board-<id>", "source_key": "calc", "title": "Calculator",
 "kind": "code", "sources": ["docs/brief.md"],
 "tasks": [
   {"key": "add", "title": "Add numbers", "outcome": "add(a, b) returns the sum.",
    "acceptance": ["add(2, 3) returns 5", "The function is documented"], "status": "ready", "priority": 1},
   {"key": "sub", "title": "Subtract numbers", "outcome": "sub(a, b) returns the difference.",
    "acceptance": ["sub(5, 3) returns 2"], "depends_on": ["add"], "status": "backlog"}]}
```

`depends_on` uses the local keys of tasks in the same import. The reply is `{"plan": {...}, "tasks": [...], "imported": true}`. Sending the same body again (the fingerprint ignores `actor`) returns the live plan and tasks with `imported: false`. Changing anything under an existing plan `source_key` in that board is `409 source_key already exists with different import content`; give a genuinely new plan a new key. The same plan key in another board is an independent plan.

## Tasks

**Create** (`/api/tasks/create`, and each import row) needs `key`, `title`, `outcome` and a non-empty `acceptance`. A new task starts in `backlog` (default) or `ready`; a `ready` task needs its dependencies done. `blocked_reason` must be empty.

**Update** (`/api/tasks/update`) changes `title`, `outcome`, `acceptance`, `depends_on`, `priority`, `owner`, `next_step`, `links`, `evidence`, `sources`, `tags`, `checklist_checks` and `correction_checks`. Status is never changed here. `attachments` and `comments` cannot be edited. A request that changes nothing is `400 no task changes supplied`.

- A task in `doing` can be edited only by its owner, with the `lease` (corrections, below, are the exception); without one the reply is `400 lease is required`, with a wrong or expired one `409`.
- **Corrections.** Changing `outcome`, `acceptance`, `depends_on` or `sources` requires a `note` and is recorded as `correct_task`. It clears `evidence`, `checklist_checks` and `corrections`, and moves a task that was `doing`, `review`, `post_validation` or `done` back to `backlog` with no owner and no claim. It needs no lease and revokes any lease. Validation history is kept. You cannot set new evidence or ticks in the same request; save the correction first.
- `depends_on` ids must be in the same plan, must exist and must not form a cycle.
- Every array field you send (`evidence`, `links`, `tags`, `acceptance`, `depends_on`, `sources`) replaces the stored array.

**Links** are `{"kind", "ref", "label"}` (`label` optional):

| Kind | `ref` | `href` |
| --- | --- | --- |
| `file` | A project-relative path (shape-checked; the file need not exist). | The artifact route. |
| `url` | `http` or `https`, no credentials. | The URL. |
| `task` | An existing task id, in any plan or board. | `/?task=ID`. |
| others | Opaque text up to 2048 characters, no control characters. Only kinds registered with `serve --link-kinds` are accepted; a kind is `[a-z][a-z0-9_-]{0,19}` and cannot repeat a core kind. | None, unless the ref is an http(s) URL. |

`GET /api/tasks` lists the active kinds in `link_kinds`. A link never changes a task's status: a linked pull request or CI run is not completion.

## Actions

`POST /api/tasks/action` takes `actor`, `id`, `revision` and `op`, plus:

| `op` | Additional fields | Effect |
| --- | --- | --- |
| `claim` | `lease_seconds`, `principles_sha256` | `ready` (or `review` with an unverified correction) becomes `doing`, owned by `actor`. Needs dependencies done, sources unchanged and, on a board with principles, the current hash. The reply carries `lease`. |
| `renew` | `lease`, `lease_seconds` | Moves `claim_expires_at` to now plus the seconds. Owner only, before expiry. |
| `release` | `lease`, `note` (required) | `doing` becomes `ready`, owner cleared, `next_step` set to the note. |
| `recover` | `note` (required) | An expired `doing` claim becomes `ready`, owner cleared, `next_step` set to the note. Any actor may do it; an unexpired claim cannot be recovered. |
| `transition` | `status`, `note`, `lease` when leaving `doing` for `review` or `post_validation`, `principles_sha256` if not acknowledged | A move from the [lifecycle table](#lifecycle). |
| `reconcile` | `note` (required) | Re-hashes the task's sources and applies the correction rules (evidence and ticks cleared; `doing`, `review`, `post_validation` and `done` go to `backlog`; `ready`, `backlog` and `blocked` keep their status). Any actor, no lease. |
| `acknowledge_principles` | `principles_sha256`, `lease` if `doing` | Records the actor's acknowledgement of the current text. |
| `validate` | `validation`, `principles_sha256` if not acknowledged | See [Post validation](#post-validation). |

## Claims and leases

A claim is atomic and revision-checked. The reply is the task and, as a **sibling** of `task` (not a field of it), the secret:

```json
{"task": {"id": "task-<id>", "status": "doing", "owner": "alice", "revision": 2,
          "claim_expires_at": 1791201227.89, "...": "..."},
 "lease": "htCQdT...43 characters"}
```

- The token is shown once, here. The server stores only its SHA-256. A caller that discards it can only wait for expiry and `recover`.
- `lease_seconds` is 60 to 3600; the default is 900.
- A write to a `doing` task must come from its `owner` and carry the token: `update`, `comment`, `attach`, `renew`, `release`, `acknowledge_principles`, and a `transition` to `review` or `post_validation`. Blocking or cancelling a claimed task, correcting its scope, reconciling it and recovering it after expiry need no token, and revoke it. This is a coordination rule between cooperating agents, not authentication.
- A token you send is always checked, in every status. After `recover`, `release`, review or a correction the old token is rejected with `409 claim lease or owner does not match`; it can never become a manual edit.
- Expiry does not change the status. After `claim_expires_at` the task is still `doing`, `renew` and writes fail with `409 claim expired; explicitly recover before claiming again`, and `recover` (with a note) returns it to `ready`. Expiry is not evidence that the worker stopped; read the last events and the next step before recovering.
- The manager reports `lease_expired` but never recovers anything.

## Evidence and the checklist

Evidence refers to a criterion by index: `{"criterion": 0, "ref": "docs/result.md", "note": "add(2, 3) evaluated to 5"}`. `ref` is free text up to 2048 characters (a path, a URL, an identifier); `note` is optional. **Sending `evidence` replaces the whole array**, so read the current list and send it back with your addition. The Python client's `set_evidence` does the merge.

Every criterion is also a checklist item. **Sending `checklist_checks` replaces the whole array**: `[{"criterion": 0, "checked": true}, ...]`, each with exactly those two keys; a criterion you leave out loses its tick. A tick (`checked: true`) requires an evidence entry for that criterion with a non-empty `ref` and a non-empty `note`. Unchanged ticks keep their original `actor` and `at`.

`correction_checks` is different: `[{"id": "correction-...", "checked": true, "note": "...", "ref": "..."}]`, all four keys, and it merges by id. Ticking a correction requires a `note` and a `ref`. Unknown or duplicate ids are `400`.

## Post validation

Moving to `post_validation` requires the execution to be complete (above). Then `validate` records the verdict:

```json
{"actor": "qa", "id": "task-<id>", "revision": 7, "op": "validate",
 "principles_sha256": "b6d20f4e...",
 "validation": {
   "result": "pass",
   "summary": "Evaluated add(2, 3) and read the docstring.",
   "checks": [
     {"criterion": 0, "passed": true, "observed": "add(2, 3) returned 5", "ref": "docs/result.md"},
     {"criterion": 1, "passed": true, "observed": "Docstring is present", "ref": "docs/result.md"}],
   "principles": [
     {"key": "terse", "passed": true, "observed": "The summary is one line", "ref": "docs/result.md"}]}}
```

- `validation` has exactly `result`, `summary`, `checks` and `principles`.
- `checks` has exactly one entry per acceptance criterion; `principles` has exactly one per current principle, by `key` (an empty array on a board without principles). Missing, unknown or duplicate entries are rejected.
- `observed` and `ref` are required on every entry. A failed entry (`passed: false`) also requires `expected` (a correct example), `avoid` (an unwanted example) and `fix` (the repair), each up to 4000 characters.
- `result` must agree with the entries: `pass` only if all passed, `fail` if any failed.
- The server re-checks that execution is complete, dependencies are done and sources are unchanged.
- Nothing requires `actor` to differ from the worker. Separating them is a convention of your process.

**Pass:** status `done`, `claim_expires_at` null, `next_step` replaced. **Fail:** status `review`; each failed entry is appended to `corrections` (unchecked, with `validation_id`); ticks for failed criteria are removed; `next_step` says to claim the repair. The record is appended to `validations`, including a snapshot of the principles it was checked against.

To repair: `claim` the task from `review` (the same principles rule applies), do the work, send `correction_checks` and re-tick the failed criteria in `checklist_checks`, move to `post_validation` and validate again. Only a fully passing validation reaches `done`.

## Comments, attachments and artifacts

**Comments** are append-only: `{"actor", "id", "revision", "kind": "note" | "conversation" | "decision", "body", "source_ref", "lease"}`. `kind` defaults to `note`; `source_ref` is optional. A `doing` task needs the lease. At most 100 per task.

**Attachments** are immutable copies kept under `<data folder>/assets/<task id>/`. Upload the raw bytes (1 byte to 32 MiB) with `Content-Length`:

```sh
curl -s -X POST --data-binary @notes.txt \
  -H 'X-Task-Revision: 7' -H 'X-Task-Actor: alice' \
  -H 'X-Task-File-Name: chat%20excerpt.txt' -H 'X-Task-File-Kind: document' \
  -H 'X-Task-Lease: LEASE-IF-DOING' \
  'http://127.0.0.1:8765/api/tasks/attach?id=TASK-ID'
```

| Header | Meaning |
| --- | --- |
| `X-Task-Revision` | The task's current revision (required). |
| `X-Task-Actor` | URI-encoded actor. |
| `X-Task-File-Name` | URI-encoded basename, up to 240 characters, no `/`, `\` or control characters. |
| `X-Task-File-Kind` | `asset` (default) or `document`. |
| `X-Task-Lease` | Required when the task is `doing`. |

The reply is `{"task": ...}` with the new entry in `attachments`; `mime` comes from the extension. If the revision is stale or the lease wrong, the staged bytes are removed. A task holds at most 100. `GET /api/tasks/attachment?task=ID&attachment=ID` returns the bytes with the stored `mime` and `Content-Disposition: attachment`. Size and SHA-256 are re-checked on every read (`409` if the stored file changed).

**Artifacts.** `GET /api/tasks/artifact?task=ID&path=PATH` returns a project file, but only if `PATH` is exactly one of that task's `file` links, evidence `ref`s or source paths. The path must be relative, with no `..`, no component starting with `.`, no symlink, and an allow-listed extension. Allowed: `.png .jpg .jpeg .webp .gif .mp4 .wav .mp3 .pdf .json` with their own types, and `.md .txt .html .svg .py .rs .js .mjs .cjs .css .toml .yaml .yml .csv` served as `text/plain`. Files over 32 MiB are refused. Anything else is `404` (not on the task) or `400` (unsafe or unserved path).

## Export

`GET /api/tasks/export` returns `{version, archival: true, history_limit: 500, boards, plans, tasks, events}`: every record in the form above and every retained event. It never contains lease tokens or hashes. It is an audit archive; nothing reads it back in. The latest 500 events per task, per plan and per board are kept; older ones are dropped.

## Errors

Every failure is `{"error": "message"}`.

| Status | Meaning |
| --- | --- |
| `400` | Invalid input: wrong type, over a limit, unknown field, missing `actor`, malformed JSON, a missing `note` or `lease`, a rule such as a dependency cycle. |
| `403` | `Host` or `Origin` is not this server (`local origin required`). |
| `404` | Unknown board, plan, task, attachment or route; an artifact not on the task. |
| `409` | A conflict with current state. Stale `revision`; lease or owner mismatch; expired claim; a transition not in the table; unmet dependencies; changed sources; missing evidence, ticks or corrections; principles not acknowledged; an idempotent create or import with different content; a duplicate key within its scope; a tampered attachment. |
| `500` | An unexpected fault. The body is `internal error; see the server log`; details go to the server's stderr. |
| `501` | An HTTP method other than `GET` or `POST`. |

```json
{"error": "stale revision; reload the current item"}
```

## Limits

| Field | Limit |
| --- | --- |
| `actor`, `owner` | 128 characters |
| `note` | 4000 |
| keys (`source_key`, `key`, principle `key`) | 160, pattern above |
| `title` (board, plan, task, principle) | 300 |
| `description` (board, plan) | 20000 |
| `scope` | 200 |
| plan `kind` | 100 (default `mixed`) |
| task `outcome` | 8000 |
| `acceptance` | 1 to 100 criteria, 2000 characters each |
| `next_step` | 4000 |
| `priority` | integer 0 to 3 (default 2) |
| `depends_on` | 100 ids, no duplicates |
| `tags` | 30, 64 characters each, no duplicates |
| `links` | 100; kind 20, `ref` 2048, `label` 300 |
| `evidence` | 200 entries; `ref` 2048, `note` 4000 |
| `sources` | 100 entries; path 1024 characters; file 16 MiB |
| `principles` | 24; content 8000 each; titles and content 64000 together |
| comment `body`, `source_ref` | 12000, 2048; 100 comments per task |
| attachments | 32 MiB each; 100 per task |
| `lease` | 200 characters accepted |
| `lease_seconds` | 60 to 3600 (default 900) |
| `revision` | integer from 1 to 2^53 - 1 |
| validation | `summary`, `observed`, `expected`, `avoid`, `fix` 4000 each; `ref` 2048; last 50 validations kept; at most 200 corrections per task |
| `correction_checks` | `note` 4000, `ref` 2048 |
| import | 1 to 1000 tasks |
| request body | 1 MB (JSON over HTTP) |
| artifact file | 32 MiB |
| history | latest 500 events per task, per plan, per board |
