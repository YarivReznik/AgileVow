"""`python -m agilevow <command>`: one entry point for the server and the three agent-side tools."""
import importlib
import sys

from . import __version__

COMMANDS = {
    "serve": ("agilevow.server", "run the board and its API on 127.0.0.1"),
    "client": ("agilevow.client", "read and write the board from a shell (claim, evidence, move, ...)"),
    "manager": ("agilevow.manager", "read-only audit: what to work on next, and what is wrong"),
    "review": ("agilevow.review", "prepare and submit a post-validation report"),
}


def usage():
    width = max(map(len, COMMANDS))
    lines = [f"agilevow {__version__}: a Kanban board for AI agents", "", "usage: python -m agilevow <command> [options]", ""]
    lines += [f"  {name:<{width}}  {text}" for name, (_, text) in COMMANDS.items()]
    lines += ["", "Run `python -m agilevow <command> --help` for a command's options."]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    if argv[0] not in COMMANDS:
        print(f"agilevow: unknown command {argv[0]!r}\n\n{usage()}", file=sys.stderr)
        return 2
    return importlib.import_module(COMMANDS[argv[0]][0]).main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
