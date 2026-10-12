"""HTTP adapter for the board: route tables and the functions behind them.

It owns no sockets. server.py hands each function a handler exposing .reply(),
.headers, .rfile and .server.task_board. Nothing here starts a worker or an agent.
"""
import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from . import board as task_board

POST_ROUTES = {
    "/api/boards/create": "create_board", "/api/boards/update": "update_board",
    "/api/plans/create": "create_plan", "/api/plans/update": "update_plan",
    "/api/plans/import": "import_plan", "/api/tasks/create": "create_task",
    "/api/tasks/update": "update_task", "/api/tasks/action": "action",
    "/api/tasks/comment": "comment",
}
UPLOAD_ROUTE = "/api/tasks/attach"
MAX_ATTACHMENT = 32 * 1024 * 1024
ARTIFACT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".pdf": "application/pdf",
    ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    # Evidence HTML is source, not a same-origin script with access to the board.
    ".html": "text/plain; charset=utf-8",
}
ARTIFACT_TYPES.update({suffix: "text/plain; charset=utf-8" for suffix in
                       (".py", ".rs", ".js", ".mjs", ".cjs", ".css", ".toml", ".yaml", ".yml", ".csv", ".svg")})


def safe_path(root, value):
    """Only explicit project-relative references, without symlink traversal."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise task_board.TaskError("A project-relative path is required", 400)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or any(p.startswith(".") for p in relative.parts):
        raise task_board.TaskError("Private or parent paths cannot be served", 400)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise task_board.TaskError("Symlink references cannot be served", 400)
    if not path.resolve().is_relative_to(root.resolve()):
        raise task_board.TaskError("Reference leaves the project", 400)
    return path


def enrich(server, task):
    result = dict(task)
    links = []
    for link in task.get("links", []):
        link = dict(link)
        kind, ref = link.get("kind"), link.get("ref")
        if kind == "file":
            link["href"] = "/api/tasks/artifact?task=" + quote(task["id"]) + "&path=" + quote(ref, safe="")
        elif isinstance(ref, str) and ref.startswith(("https://", "http://")) and kind != "task":
            link["href"] = ref
        elif kind == "task":
            link["href"] = "/?task=" + quote(ref, safe="")
        links.append(link)
    result["links"] = links
    result["attachments"] = [dict(item, href="/api/tasks/attachment?task=" + quote(task["id"]) +
                                   "&attachment=" + quote(item["id"]))
                             for item in task.get("attachments", [])]
    result["sources"] = [dict(source, href="/api/tasks/artifact?task=" + quote(task["id"]) +
                              "&path=" + quote(source["path"], safe=""))
                         for source in task.get("sources", [])]
    result["evidence"] = []
    for value in task.get("evidence", []):
        value = dict(value)
        ref = value["ref"]
        if ref.startswith(("https://", "http://")):
            value["href"] = ref
        else:
            try:
                path = safe_path(server.task_board.root, ref)
                if path.suffix.lower() in ARTIFACT_TYPES:
                    value["href"] = "/api/tasks/artifact?task=" + quote(task["id"]) + "&path=" + quote(ref, safe="")
            except task_board.TaskError:
                pass
        result["evidence"].append(value)
    return result


def attachment_path(board, task_id, attachment_id):
    if (not isinstance(task_id, str) or not re.fullmatch(r"task-[a-f0-9]{32}", task_id) or
            not isinstance(attachment_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attachment_id)):
        raise task_board.TaskError("Invalid attachment identity", 400)
    path = board.root
    for part in (*board.data_dir.split("/"), task_board.ASSETS_DIR, task_id, attachment_id):
        path = path / part
        if path.is_symlink():
            raise task_board.TaskError("Attachment storage must not contain symlinks", 400)
    return path


def upload(handler, url, size):
    """A blob is retained only if the revision-checked metadata transaction succeeds."""
    if not 0 < size <= MAX_ATTACHMENT:
        raise task_board.TaskError("Choose an attachment between 1 byte and 32 MiB", 400)
    query = parse_qs(url.query)
    task_id = query.get("id", [None])[0]
    name = unquote(handler.headers.get("X-Task-File-Name", ""))
    if (not name or len(name) > 240 or name in (".", "..") or
            any(c in name for c in ("/", "\\", "\x00", "\r", "\n"))):
        raise task_board.TaskError("A filename without directories is required", 400)
    kind = handler.headers.get("X-Task-File-Kind", "asset")
    if kind not in ("asset", "document"):
        raise task_board.TaskError("Attachment kind must be asset or document", 400)
    board = handler.server.task_board
    board.get_task(task_id)  # Fail before accepting bytes for an unknown task.
    ident = uuid.uuid4().hex
    path = attachment_path(board, task_id, ident)
    data = handler.rfile.read(size)
    if len(data) != size:
        raise task_board.TaskError("Incomplete attachment body", 400)
    suffix = Path(name).suffix.lower()
    mime = ARTIFACT_TYPES.get(suffix, "application/octet-stream")
    request = {"id": task_id, "revision": int(handler.headers.get("X-Task-Revision", "-1")),
               "actor": unquote(handler.headers.get("X-Task-Actor", "")),
               "lease": handler.headers.get("X-Task-Lease"),
               "attachment": {"id": ident, "filename": name, "kind": kind,
                              "sha256": hashlib.sha256(data).hexdigest(), "size": size,
                              "mime": mime, "path": str(path.relative_to(board.root))}}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        result = board.attach(request)
    except Exception:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()  # only succeeds when nothing else is stored for this task
        except OSError:
            pass
        raise
    result["task"] = enrich(handler.server, result["task"])
    handler.reply(200, result)


def attachment(server, task_id, ident):
    task = server.task_board.get_task(task_id)["task"]
    item = next((value for value in task.get("attachments", []) if value["id"] == ident), None)
    if not item:
        raise task_board.TaskError("Attachment is not on this task", 404)
    path = attachment_path(server.task_board, task_id, ident)
    if not path.is_file():
        raise task_board.TaskError("Attachment bytes are missing", 404)
    if path.stat().st_size != item["size"] or item["size"] > MAX_ATTACHMENT:
        raise task_board.TaskError("Attachment bytes changed", 409)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise task_board.TaskError("Attachment fingerprint changed", 409)
    return data, item


def artifact(server, task_id, name):
    result = server.task_board.get_task(task_id)
    task = result["task"]
    allowed = {link["ref"] for link in task.get("links", []) if link.get("kind") == "file"}
    allowed.update(e["ref"] for e in task.get("evidence", []))
    allowed.update(s["path"] for s in task.get("sources", []))
    if name not in allowed:
        raise task_board.TaskError("File is not attached to this task", 404)
    path = safe_path(server.task_board.root, name)
    if path.suffix.lower() not in ARTIFACT_TYPES:
        raise task_board.TaskError("This artifact type is not served; open the file directly", 400)
    if not path.is_file():
        raise task_board.TaskError("Attached artifact is missing", 404)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise task_board.TaskError("Artifacts over 32 MiB are not served; open the file directly", 400)
    return path.read_bytes(), ARTIFACT_TYPES[path.suffix.lower()]


def get(handler, url):
    if not url.path.startswith(("/api/tasks", "/api/boards")):
        return False
    board = handler.server.task_board
    query = parse_qs(url.query)
    param = lambda key: query.get(key, [None])[0]
    if url.path == "/api/tasks":
        result = board.snapshot(plan_id=param("plan"), board_id=param("board"))
        result["tasks"] = [enrich(handler.server, task) for task in result["tasks"]]
    elif url.path == "/api/boards":
        result = {"boards": board.snapshot()["boards"]}
    elif url.path == "/api/tasks/version":
        result = {"version": board.change_marker()}
    elif url.path == "/api/tasks/item":
        result = board.get_task(param("id"))
        result["task"] = enrich(handler.server, result["task"])
    elif url.path == "/api/tasks/export":
        result = board.export()
    elif url.path == "/api/tasks/attachment":
        data, item = attachment(handler.server, param("task"), param("attachment"))
        handler.reply(200, data, item["mime"],
                      {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(item["filename"], safe="")})
        return True
    elif url.path == "/api/tasks/artifact":
        data, mime = artifact(handler.server, param("task"), param("path"))
        handler.reply(200, data, mime)
        return True
    else:
        handler.reply(404, {"error": "Unknown task API route"})
        return True
    handler.reply(200, result)
    return True


def post(handler, url, request):
    method = POST_ROUTES.get(url.path)
    if method is None:
        return False
    result = getattr(handler.server.task_board, method)(request)
    if "task" in result:
        result["task"] = enrich(handler.server, result["task"])
    handler.reply(200, result)
    return True
