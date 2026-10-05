"""The light and dark themes live in board.css as custom properties. Three things can silently go wrong with
that, and none of them shows up in a browser test that only checks the light theme:

* the dark theme is written twice (once for "the system prefers dark", once for "the visitor chose dark")
  and the two copies drift apart;
* a token is added for the light theme and never redefined for the dark one, so dark shows a light patch;
* a pair of colours that reads well in one theme is unreadable in the other.

These tests read the style sheet as text. They need no browser."""
import re
import unittest
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "agilevow" / "web" / "board.css").read_text(encoding="utf-8")

# Tokens that are the same in both themes on purpose: sizes, and the solid avatar colours (white initials on a
# saturated disc read the same on a light or a dark page).
SAME_IN_BOTH = {"radius", "radius-lg", "av-backlog", "av-ready", "av-doing", "av-review", "av-post_validation",
                "av-done", "av-blocked", "av-cancelled"}
# Set per element by the page's own rules or script, never in :root.
SET_ELSEWHERE = {"p", "status-columns", "tint", "tint-top", "accent", "av"}


def block(start, end="\n}\n"):
    a = CSS.index(start)
    b = CSS.index(end, a)
    return CSS[a + len(start):b]


def declarations(text):
    return {m.group(1): " ".join(m.group(2).split()) for m in re.finditer(r"--([\w-]+):\s*([^;]+);", text)}


LIGHT = declarations(block(":root {"))
DARK = declarations(block(':root[data-theme="dark"] {'))
MEDIA = declarations(block('@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {', "\n  }\n}\n"))


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ThemeTokens(unittest.TestCase):
    def test_the_two_dark_blocks_are_identical(self):
        self.assertEqual(MEDIA, DARK, "the prefers-color-scheme block and the data-theme block must hold the same tokens")
        self.assertGreater(len(DARK), 50)

    def test_every_light_token_is_redefined_for_dark(self):
        missing = sorted(set(LIGHT) - set(DARK) - SAME_IN_BOTH)
        self.assertEqual(missing, [], f"tokens that only the light theme defines (dark would show a light patch): {missing}")

    def test_dark_defines_nothing_the_light_theme_lacks(self):
        self.assertEqual(sorted(set(DARK) - set(LIGHT)), [])

    def test_every_token_in_use_is_defined(self):
        used = set(re.findall(r"var\(--([\w-]+)", CSS))
        missing = sorted(used - set(LIGHT) - SET_ELSEWHERE)
        self.assertEqual(missing, [], f"var(--x) with no definition: {missing}")

    def test_status_tints_are_all_different_in_each_theme(self):
        for name, tokens in (("light", LIGHT), ("dark", DARK)):
            tints = [v for k, v in tokens.items() if re.fullmatch(r"t-[a-z_]+", k)]
            self.assertEqual(len(tints), 8)
            self.assertEqual(len(set(tints)), 8, f"two status columns share a tint in the {name} theme")


class ThemeContrast(unittest.TestCase):
    """WCAG 2.x contrast. 4.5:1 for text that carries meaning, 3:1 for the faint secondary text and placeholders."""

    PAIRS = [
        # (foreground, [backgrounds], minimum)
        ("ink", ["surface", "bg", "panel", "principles", "chip", "blue-soft", "nav-active", "sidebar",
                 "t-backlog", "t-ready", "t-doing", "t-review", "t-post_validation", "t-done", "t-blocked", "t-cancelled"], 4.5),
        ("ink-2", ["surface", "bg", "panel", "sidebar", "chip"], 4.5),
        ("muted", ["surface", "bg", "panel", "principles", "chip", "sidebar",
                   "t-backlog", "t-ready", "t-doing", "t-review", "t-post_validation", "t-done", "t-blocked", "t-cancelled"], 4.5),
        ("faint", ["surface", "bg"], 3.0),
        ("link", ["surface", "bg", "panel"], 4.5),
        ("blue-ink", ["blue-soft", "count-bg"], 4.5),
        ("red-ink", ["red-bg"], 4.5),
        ("amber-ink", ["amber-bg"], 4.5),
        ("green-ink", ["green-bg"], 4.5),
    ]

    def check(self, theme, tokens):
        failures = []
        for fg, backgrounds, minimum in self.PAIRS:
            for bg in backgrounds:
                ratio = contrast(tokens[fg], tokens[bg])
                if ratio < minimum:
                    failures.append(f"{theme}: {fg} {tokens[fg]} on {bg} {tokens[bg]} is {ratio:.2f}:1, needs {minimum}")
        for state in ("blue", "blue-hover"):       # the primary button at rest and under the pointer
            ratio = contrast("#ffffff", tokens[state])
            if ratio < 4.5:
                failures.append(f"{theme}: white text on the primary button ({state}) {tokens[state]} is {ratio:.2f}:1, needs 4.5")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_light(self):
        self.check("light", LIGHT)

    def test_dark(self):
        self.check("dark", DARK)


if __name__ == "__main__":
    unittest.main()
