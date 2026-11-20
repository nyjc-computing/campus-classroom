"""Markdown filter contract (text_render.render_markdown).

Staff-authored prompts/passages/descriptions render as markdown on the
display surfaces. The contract:

  1. Common markdown renders: **bold**, "- " lists, headings, fences.
  2. Single newlines become <br> (nl2br) — verbatim-authored text
     keeps its line structure (#115).
  3. Parse-then-sanitize: script tags/event handlers/javascript: URLs
     never survive; benign raw HTML does.
  4. Empty/None renders as empty string.

Run:  python tests/test_markdown_filter.py   (exit 1 on violation)
"""

import importlib.util
import sys
from pathlib import Path

# Load by file path: importing apps.classroom pulls the whole app
# factory (campus_python, flask_campus); the filter itself only needs
# the `markdown` package, and this test runs with just that installed.
_spec = importlib.util.spec_from_file_location(
    "text_render_under_test",
    Path(__file__).resolve().parents[1] / "apps" / "classroom" / "text_render.py",
)
_text_render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_text_render)
render_markdown = _text_render.render_markdown

checks = []


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    checks.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got: {got!r}"))


out = render_markdown("**Your goal:** Complete the quest.")
check("bold renders as strong", out, lambda o: "<strong>Your goal:</strong>" in o)

out = render_markdown("- HTML represents content\n- CSS represents formatting")
check(
    "dash lines render as a list",
    out,
    lambda o: "<ul>" in o and "<li>HTML represents content</li>" in o,
)

out = render_markdown("In this lesson, we focus on HTML.\nUse the viewer below.")
check("single newline renders as <br> (nl2br)", out, lambda o: "<br" in o)

out = render_markdown("Paragraph one.\n\nParagraph two.")
check("blank line splits paragraphs", out, lambda o: o.count("<p>") == 2)

out = render_markdown("<script>alert(1)</script>")
check(
    "script tags removed with their content",
    out,
    lambda o: "alert(1)" not in o and "<script" not in o,
)

out = render_markdown('<img src="x" onerror="alert(1)">')
check(
    "event-handler attributes stripped",
    out,
    lambda o: "onerror" not in o and "alert(1)" not in o,
)

out = render_markdown("<b>raw bold</b>")
check("benign raw HTML survives", out, lambda o: "<b>raw bold</b>" in o)

out = render_markdown("[link](javascript:alert(1))")
check("javascript: URLs sanitized", out, lambda o: "javascript:" not in o)

out = render_markdown("# Heading\nBody text.")
check("headings render", out, lambda o: "<h1>Heading</h1>" in o)

out = render_markdown("```html\n<p>inline</p>\n```")
check(
    "fenced code displays its literal content",
    out,
    lambda o: "&lt;p&gt;inline&lt;/p&gt;" in o and "class=\"language-" not in o,
)

check("empty string renders empty", render_markdown(""), "")
check("None renders empty", render_markdown(None), "")

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed.")
if failed:
    sys.exit(1)
