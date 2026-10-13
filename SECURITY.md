# Security

AgileVow is a **single-user local tool**. It coordinates agents and people who are already trusted on one machine. It is not a multi-user service and has no accounts. Read this page before you point anything at it that you do not trust.

## What it assumes

- Everyone who can open its port is trusted to read and write the whole board.
- Every process running as you is trusted. Malware under your account can read the database and your lease file.
- The machine is not shared with other users you do not trust (see "No authentication").

## The network boundary

- The server binds `127.0.0.1` only. There is no option to bind another address, and no TLS.
- Every request is checked before anything else. `Host` must be exactly `localhost:PORT` or `127.0.0.1:PORT` for the port it listens on, and `Origin`, if the request has one, must be `http://` followed by one of those. Anything else is `403 local origin required`. A request with no `Host` (HTTP/1.0) is also refused.
- This stops DNS rebinding (a rebound name arrives with a foreign `Host`) and cross-site form posts or fetches (a browser sends `Origin` on a cross-origin `POST`). The server sends no CORS headers, so another site cannot read its responses.
- It also means these do not work, by design: reverse proxies that rewrite `Host`, Docker port forwards, Codespaces and similar forwarded URLs, other machines on your network, and `[::1]`. Use `127.0.0.1` or `localhost`.
- Programs that are not browsers can send any `Host` and `Origin`. The check protects you from web pages, not from local programs.

## No authentication

- There are no users, passwords or tokens for the API. `actor` is text the caller asserts (up to 128 characters) and is recorded, not verified.
- A **lease** (the token returned by `claim`) stops one cooperating agent from overwriting another's work on a card in Doing. It does not authenticate callers: it is a coordination rule. Without the token anyone can still block or cancel a claimed card, correct its scope, reconcile it, comment on a card that is not in Doing, recover it after expiry, and run `validate` on a card in Post validation. Nothing requires the validator to differ from the worker.
- Loopback is shared by all users of the machine. On a multi-user host, any local user can connect to the port and read and write the board, whatever the file permissions say. Do not run AgileVow there.

## What an API caller can reach in your files

Task sources, evidence and `file` links name files under `--root`. Treat `--root` as the boundary of what the board may touch, and give it a folder that holds only what you are willing to expose, not your home folder.

- **Sources.** Any path under `--root` that is a regular file of at most 16 MiB, with no symlink in the path and no `..`, absolute or backslash form, can be added to a plan or task. The server reads it and stores its SHA-256, and shows the hash again on every read. Dotfiles are allowed here. The content is not returned, but a hash lets someone test guesses about a file's content.
- **Reading a file back.** `GET /api/tasks/artifact?task=ID&path=PATH` returns a file's bytes only if `PATH` is, character for character, one of that task's `file` links, evidence references or source paths. That is not a barrier for a caller who can write to the board, because adding a link to a task is a normal write. So the real limits are on the path itself: relative, no `..`, no path component starting with `.` (this excludes `.git`, `.env` and AgileVow's own `.agilevow`), no symlink anywhere in the path, the resolved path inside `--root`, an existing file of at most 32 MiB, and an extension on the allow-list: `png jpg jpeg webp gif mp4 wav mp3 pdf json md txt html svg py rs js mjs cjs css toml yaml yml csv`. A `config.yaml` or `secrets.json` under `--root` is therefore readable by anyone who can reach the port. Files outside `--root`, dotfiles, symlinks and other extensions are not.
- The artifact route refuses symlinks by checking each path component, then reads the file. Source hashing and attachment fingerprinting walk the path with `O_NOFOLLOW`, one open file descriptor per folder, instead. A local process that can change your project tree while a request runs can race the first; one that can do that already owns the tree.
- **Attachments** are stored under `<data folder>/assets/<task id>/<random id>` with server-chosen names. The uploaded filename is metadata only and never becomes a path.
- The server makes no outbound connections and runs nothing taken from task content. SQL uses bound parameters.

## Data on disk

| What | Where | Permissions |
| --- | --- | --- |
| Data folder | `<root>/.agilevow/` (or `--data-dir`) | `0700`, created with a `.gitignore` containing `*`, but only if it did not exist; an existing folder is left as it was |
| Database | `<data folder>/agilevow.sqlite3` | `0600`, set on every start |
| Attachment bytes | `<data folder>/assets/<task id>/<id>` | `0600` |
| Lease tokens | `./.agilevow/leases.json`, or `AGILEVOW_LEASES`, on the client side | `0600` |

- Each task's folder under `assets/` is created `0700` (your umask can only tighten that), and is removed again if the upload that created it is rejected.
- The lease file is plain text: the token is the credential. Its folder is created with your umask and no `.gitignore` if it does not exist. Run `client` from the project root, where the data folder is already private and ignored, or point `AGILEVOW_LEASES` at a private place.
- The server keeps only the SHA-256 of each lease token, and compares in constant time. Tokens, and their hashes, never appear in a read, a snapshot or an export.
- Nothing is encrypted. The database holds all task text, comments, evidence references and principles; the export holds the same and is readable by anyone who can reach the port. Back up and share it accordingly.
- The web page keeps your actor name in `localStorage` and lease tokens in `sessionStorage` (per tab).

## The page

- The page is served with `Content-Security-Policy: default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`. There are no inline scripts or styles.
- Board text (titles, comments, principles, evidence) is rendered as text. The page's code contains no `innerHTML`, and the CSP would block an injected script in any case.
- Every response carries `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- HTML and SVG evidence, and source-code files, are served as `text/plain`, so a project file cannot run as a page on the board's origin. Images, PDF, audio and video are served with their own types; `.json` as `application/json`. Attachments are always sent with `Content-Disposition: attachment`; an unknown extension is `application/octet-stream`.
- The CSP and `X-Frame-Options` are on the page only. Chrome shows a blank frame for a PDF sent with `X-Frame-Options: DENY`, and artifacts and attachments are served from the same origin.

## Resource use

- JSON bodies are limited to 1 MB; every `POST` needs a `Content-Length`.
- An upload is read into memory in full (up to 32 MiB) before it is written, and an artifact or attachment is read into memory to be served. The server runs one thread per connection with no cap and sets no socket timeout, so a stalled client keeps a thread. This is a local tool; do not expose it.
- Writes are SQLite transactions (`BEGIN IMMEDIATE`, 15 s busy timeout), so several server processes on one database are safe, but each write is serialised.

## Errors

An unexpected exception is logged to the server's stderr with a traceback and the client gets only `internal error; see the server log` (`--quiet` does not hide it). Errors about the request itself (validation, conflicts, bad JSON) are returned with their message, which can include a path or value the caller sent.

## Platform

POSIX only (macOS, Linux). Source and attachment paths are walked with `O_NOFOLLOW` and `dir_fd` opens, which Windows does not offer. On a system without them `serve` exits with an error rather than running with weaker checks.

## The MCP server

`agilevow mcp` is a child process of whatever launches it (an agent client) and talks to `AGILEVOW_URL` over HTTP, which is `127.0.0.1` unless you change it. It adds no listening socket and no new authority: whoever can launch it has the powers of the actor name it is given, the same as anyone who can reach the board's port.

- Arguments come from a model and are treated as untrusted: they are checked against each tool's schema, and the board server's own checks remain the authority.
- Lease tokens are never returned to the model. They are kept in `~/.agilevow/leases-ACTOR.json` (folder `0700`, file `0600`) unless `--leases` says otherwise.
- `create_board` exists so a user can set up a board from a chat; its description tells the agent to use only the principles the user stated. Nothing over MCP can change a board's principles afterwards (a second `create_board` with different content is refused and there is no update tool), so an agent cannot rewrite the rules it is judged against once work has started. An agent could still put words of its own into a new board; read a board's principles before relying on them. It can import a plan into an existing board; a plan's `sources` can name any file under the server's `--root`, exactly as through the API (see "What an API caller can reach in your files").
- `review submit` on the command line and `submit_review` over MCP both sign a review with the actor running them (`AGILEVOW_ACTOR`, `--actor`, or the MCP server's actor) and refuse a report signed with another name. `submit_review` validates as the actor this MCP server runs as, whatever name the report types, and the board refuses a passing validation from anyone who worked on the card. An agent cannot pass its own work by renaming itself in the report; a second MCP server with a different `AGILEVOW_ACTOR`, or a person, has to. Names are not authenticated, so this guards against the lazy default, not against an operator who configures an agent to lie.
- Two MCP servers with different actors registered in **one** client give one agent both names; the board cannot tell. The worker is instructed never to pass its own card through another AgileVow server, but that is an instruction, not a control. Keep the reviewer in a separate session (`claude --strict-mcp-config --mcp-config examples/agilevow-review.mcp.json`) or review yourself.
- Every card records who wrote its acceptance criteria (`criteria_by`, set by the server, not by a request). A worker reviewing against criteria it wrote itself is flagged on the card, in `audit_board` and in the review report, not refused.
- The process writes protocol messages to stdout and everything else to stderr. A tool result larger than 100,000 characters is truncated.

## Dependencies

None at runtime: the standard library only. `package.json` lists Playwright for the browser test and is not used when you run AgileVow.

## Reporting a vulnerability

Open a private security advisory on this repository on GitHub (the Security tab, "Report a vulnerability"). Please include the version (`python3 -m agilevow --version`), how you ran it, and the request or steps that show the problem. Do not file public issues for vulnerabilities.
