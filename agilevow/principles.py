"""The principle a new board starts with: the people and agents on it say what they are doing as they do it.

The web page offers it, ticked, when a board is created (its text is translated in web/i18n.mjs, and
tests/test_principles.py keeps the English copy identical to this one), and the demo board carries it.
A principle is binding: an agent quotes its hash to claim a card, and the reviewer passes or fails it
before the card can be Done, so "did they report?" is checked, not hoped for.
"""

REPORT_AS_YOU_WORK = {
    "key": "report-as-you-work",
    "title": "Report as you work",
    "content": ("Keep the card telling the story. When you claim it, comment what you are about to do. "
                "Comment each meaningful step, finding or decision as it happens, not at the end. "
                "When anything changes (the plan, the scope, a blocker, a dependency), comment what changed and why "
                "before you go on. Keep the next step current, and before you stop or hand over, leave it so that "
                "someone else could pick the card up."),
}
