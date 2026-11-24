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


import re
from html.parser import HTMLParser

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


class _PlainTextInput(HTMLParser):
    """Text-node collector that keeps link destinations readable.

    Plain text cannot carry <a href>, so a markdown link
    [text](url) survives as ``text (url)`` — otherwise instructions
    like "see the guide" would lose the destination entirely. Block
    tags emit paragraph separators: python-markdown only separates
    sibling blocks with a single newline, which would read as one wall
    of text once the tags are stripped.
    """

    # Line break inside a soft structure (list items keep together);
    # paragraph break around real blocks.
    _BLOCK_SEPS = {
        "p": "\n\n", "h1": "\n\n", "h2": "\n\n", "h3": "\n\n",
        "h4": "\n\n", "h5": "\n\n", "h6": "\n\n", "blockquote": "\n\n",
        "pre": "\n\n", "div": "\n\n", "tr": "\n\n", "li": "\n",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open_hrefs: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._open_hrefs.append(dict(attrs).get("href"))
            # Marker: link text accumulates into its own part.
            self.parts.append("")
        elif tag in self._BLOCK_SEPS:
            self.parts.append(self._BLOCK_SEPS[tag])

    def handle_endtag(self, tag):
        if tag == "a" and self._open_hrefs:
            href = self._open_hrefs.pop()
            link_text = self.parts.pop()
            if href and href != link_text:
                link_text += f" ({href})"
            self.parts.append(link_text)

    def handle_data(self, data):
        if self._open_hrefs:
            self.parts[-1] += data
        else:
            self.parts.append(data)


def markdown_to_plain(text: str | None) -> str:
    """Staff-authored markdown as plain text.

    For surfaces that cannot render markup (Google Classroom
    instructions are plain text): the same parse used for display gets
    flattened — tags stripped, entities decoded, link destinations kept
    — then runs of blank lines collapse to one paragraph break.
    """
    if not text:
        return ""
    parser = _PlainTextInput()
    parser.feed(render_markdown(text))
    parser.close()
    lines = [re.sub(r"[ \t]+", " ", line).strip()
             for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


__all__ = ["markdown_to_plain", "render_markdown"]
