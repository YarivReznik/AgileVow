"""The default principle a new board starts with is written once in agilevow/principles.py, in English. The web page
holds the same text as a constant (not a translation: principles are what agents read and are checked against, so
the stored words do not depend on the language the page is shown in), and the two must not drift apart."""
import re
import unittest
from pathlib import Path

from agilevow.principles import REPORT_AS_YOU_WORK

WEB = Path(__file__).resolve().parent.parent / "agilevow" / "web"
QUOTED = r"'((?:[^'\\]|\\.)*)'"


def page_constant():
    text = (WEB / "board.mjs").read_text(encoding="utf-8")
    found = re.search(rf"const REPORT_AS_YOU_WORK = {{key:{QUOTED},title:{QUOTED},content:{QUOTED}}};", text)
    return dict(zip(("key", "title", "content"), (v.replace("\\'", "'") for v in found.groups())))


class DefaultPrincipleTests(unittest.TestCase):
    def test_the_page_says_exactly_what_the_python_copy_says(self):
        self.assertEqual(page_constant(), REPORT_AS_YOU_WORK)

    def test_its_wording_is_never_translated(self):
        strings = (WEB / "i18n.mjs").read_text(encoding="utf-8")
        self.assertNotIn("principles.defaultContent", strings)
        self.assertNotIn("principles.defaultTitle", strings)
        self.assertNotIn(REPORT_AS_YOU_WORK["content"][:40], strings)


if __name__ == "__main__":
    unittest.main()
