"""campus.apps.classroom.text_render

Markdown rendering for staff-authored text (question prompts, group
passages, question text, assignment description).

Parse-then-sanitize: python-markdown renders the full syntax (the
fenced-code content escapes itself), then nh3 strips anything
dangerous — script/style tags with their contents, event-handler
attributes, javascript: URLs — with an allowlist. Prompts are teacher-
authored but student-visible, so a compromised or careless staff
account must not be able to script students' browsers; benign raw HTML
(bold, links) survives. `nl2br` keeps the verbatim-authored line
structure (#115): single newlines render as line breaks, blank lines
still split paragraphs, and "- " lines form lists.
"""


import markdown
import nh3

_EXTENSIONS = ("nl2br", "fenced_code")


def render_markdown(text: str | None) -> str:
    """Render staff-authored markdown text to a sanitized HTML string.

    The result is safe to insert unescaped (Jinja: pass through the
    `md` filter, which returns markup).
    """
    if not text:
        return ""
    return nh3.clean(
        markdown.markdown(str(text), extensions=_EXTENSIONS)
    )


__all__ = ["render_markdown"]
