"""The AgileVow server: the board's JSON API and its single page, on loopback only.

    python -m agilevow serve --root /path/to/project

AgileVow is a single-user local tool. It binds 127.0.0.1, refuses any Host or Origin
that is not that address and port, and has no accounts: see SECURITY.md.
"""
import argparse
import json
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import api, board as task_board

WEB = Path(__file__).with_name("web")
# The only files this server will ever serve as pages. Everything else is the API.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/board.css": ("board.css", "text/css; charset=utf-8"),
    "/board.mjs": ("board.mjs", "text/javascript; charset=utf-8"),
    "/i18n.mjs": ("i18n.mjs", "text/javascript; charset=utf-8"),
}
# Applied to the page only: Chrome's PDF viewer shows a blank frame for a PDF served with
# X-Frame-Options DENY, and attachments are served from the same origin.
PAGE_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; style-src 'self'; script-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
MAX_JSON = 1024 * 1024
DEFAULT_PORT = 8765


def _no_constants(token):
    raise ValueError("non-finite JSON number")


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, content, mime="application/json", headers=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        if not getattr(self.server, "quiet", False):
            super().log_message(format, *args)

    def permitted(self):
        """Loopback only. This also defeats DNS rebinding: a rebound name arrives with a foreign Host."""
        port = self.server.server_port
        allowed = {f"localhost:{port}", f"127.0.0.1:{port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host", "") in allowed and (origin is None or origin in {"http://" + h for h in allowed})

    def fail(self, error, client_errors):
        """Client mistakes explain themselves; anything else is logged here and not echoed back."""
        if isinstance(error, task_board.TaskError):
            return self.reply(error.status, {"error": str(error)})
        if isinstance(error, client_errors):
            return self.reply(400, {"error": str(error)})
        traceback.print_exc(file=sys.stderr)
        return self.reply(500, {"error": "internal error; see the server log"})

    def do_GET(self):
        if not self.permitted():
            return self.reply(403, {"error": "local origin required"})
        url = urlsplit(self.path)
        try:
            if api.get(self, url):
                return
            page = STATIC.get(url.path)
            if page is None:
                return self.reply(404, {"error": "not found"})
            name, mime = page
            return self.reply(200, (WEB / name).read_bytes(), mime, PAGE_HEADERS)
        except Exception as error:
            return self.fail(error, (ValueError, KeyError))

    def do_POST(self):
        if not self.permitted():
            return self.reply(403, {"error": "local origin required"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            url = urlsplit(self.path)
            if url.path == api.UPLOAD_ROUTE:
                return api.upload(self, url, size)
            if not 0 < size <= MAX_JSON:
                raise ValueError("request must be 1 byte to 1 MB")
            request = json.loads(self.rfile.read(size), parse_constant=_no_constants)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            if api.post(self, url, request):
                return
            return self.reply(404, {"error": "unknown operation"})
        except Exception as error:
            return self.fail(error, (ValueError, KeyError, TypeError))


def make_server(root, port=DEFAULT_PORT, data_dir=task_board.DATA_DIR, link_kinds=(), clock=time.time, quiet=False):
    """A bound, not yet serving, server. Port 0 picks a free port (tests do)."""
    board = task_board.TaskBoard(root, data_dir=data_dir, clock=clock, link_kinds=link_kinds)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.task_board = board
    server.quiet = quiet
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agilevow serve", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="project directory that task sources and evidence are fingerprinted against (default: current directory)")
    parser.add_argument("--data-dir", default=task_board.DATA_DIR,
                        help="database and attachments directory, relative to --root (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="default: %(default)s")
    parser.add_argument("--quiet", action="store_true", help="do not log each request")
    parser.add_argument("--link-kinds", default="",
                        help="extra link kinds, comma separated (for example: pr,issue,commit); file, url and task always exist")
    args = parser.parse_args(argv)
    kinds = [kind.strip() for kind in args.link_kinds.split(",") if kind.strip()]
    try:
        server = make_server(args.root, args.port, args.data_dir, kinds, quiet=args.quiet)
    except (task_board.TaskError, RuntimeError) as error:
        print(f"agilevow: {error}", file=sys.stderr)
        return 2
    except OSError as error:  # port already in use, a project folder that cannot be written, ...
        print(f"agilevow: cannot start on 127.0.0.1:{args.port}: {error.strerror or error}", file=sys.stderr)
        return 2
    print(f"AgileVow: http://127.0.0.1:{server.server_port}/\nProject: {server.task_board.root}\n"
          f"Data:    {server.task_board.root / server.task_board.data_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
