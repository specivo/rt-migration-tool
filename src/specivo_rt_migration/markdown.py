"""Message text to Markdown: HTML converted and sanitised, plain text kept as written.

**HTML** (a reply written in RT's rich text editor, an HTML mail) is cleaned
with ``nh3`` to the tags and attributes Specivo renders, the way the Redmine
tool does it, and converted with ``markdownify``. Script and style content is
dropped. An ``<img>`` whose source is one of the message's files (by
``cid:``, or by RT's ``/Ticket/Attachment/...`` address) becomes an image
placeholder for that file.

**Plain text** (most mail) is kept as written: characters Markdown would read
as formatting are escaped, and each line break is kept as a hard break, so
the text reads as it did in RT.

In both, text that looks like a placeholder (``{{...}}``) is escaped, so only
the placeholders the tool writes are expanded, and references to other
tickets are linked by :mod:`specivo_rt_migration.references` afterwards.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

import nh3
from markdownify import MarkdownConverter

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p", "br", "strong", "b", "em", "i", "u", "s", "del", "ins", "code", "pre", "blockquote",
        "ul", "ol", "li", "a", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "thead", "tbody",
        "tr", "th", "td", "img", "div", "span", "mark", "sup", "sub", "dl", "dt", "dd",
    }
)  # fmt: skip
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "th": frozenset({"align", "colspan", "rowspan"}),
    "td": frozenset({"align", "colspan", "rowspan"}),
    "code": frozenset({"class"}),
    "pre": frozenset({"class"}),
}
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# Where an inline image points while the HTML is converted: a URL nh3 lets
# through, replaced by the attachment placeholder afterwards. Its host holds a
# digest of the message being converted, which the message cannot contain, so
# a mail written to look like the marker (even with entities) stays a plain
# link and never names another file.
_IMAGE_HOST = "specivo-attachment"
_CID_SRC_RE = re.compile(r"""(src\s*=\s*["']?)cid:([^"'\s>]+)""", re.IGNORECASE)
# RT's own address of an attachment, in a link or an image it shows inline:
# .../Ticket/Attachment/<transaction>/<attachment>/<name>.
_RT_ATTACHMENT_URL_RE = re.compile(
    r"""((?:src|href)\s*=\s*["']?)[^"'\s>]*?/Ticket/Attachment/(?:WithHeaders/)?\d+/(\d+)/?[^"'\s>]*""",
    re.IGNORECASE,
)

# A "<" that would open a tag, a comment or a declaration in Markdown.
_TAG_OPEN_RE = re.compile(r"<(?=[A-Za-z/!?])")
_PLACEHOLDER_LIKE_RE = re.compile(r"\{\{")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
# An image left without a source (a cid: no file of the message answers): nothing to show.
_EMPTY_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\(\s*\)")

# Plain text: characters Markdown reads as formatting anywhere in a line.
_INLINE_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>|~])")
# ... and at the start of a line: headings, lists, quotes, rules, tables.
_LINE_START_RE = re.compile(r"^(\s*)([#>+=-]|\d+[.)])")


class _MarkdownConverter(MarkdownConverter):
    """markdownify, with tag-like text kept as text."""

    def escape(self, text: str, parent_tags: set[str]) -> str:
        escaped = str(super().escape(text, parent_tags))  # type: ignore[misc]
        return _TAG_OPEN_RE.sub("&lt;", escaped)


def html_to_markdown(html: str, attachment_ref: Callable[[str], str | None]) -> str:
    """Convert an HTML message to Markdown.

    *attachment_ref* maps a ``cid:`` target or an RT attachment id (as text)
    to the bundle ref of the file it names, or ``None``.
    """

    digest = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()[:32]
    image_base = f"https://{_IMAGE_HOST}-{digest}.invalid/"
    image_url_re = re.compile(re.escape(image_base) + r"([^\s)\"'>]+)")

    def on_cid(match: re.Match[str]) -> str:
        ref = attachment_ref(match.group(2))
        return f"{match.group(1)}{image_base}{ref}" if ref else match.group(0)

    def on_rt_url(match: re.Match[str]) -> str:
        ref = attachment_ref(match.group(2))
        return f"{match.group(1)}{image_base}{ref}" if ref else match.group(0)

    html = _CID_SRC_RE.sub(on_cid, html)
    html = _RT_ATTACHMENT_URL_RE.sub(on_rt_url, html)
    cleaned = nh3.clean(
        html,
        tags=set(ALLOWED_TAGS),
        attributes={tag: set(names) for tag, names in ALLOWED_ATTRIBUTES.items()},
        url_schemes=set(ALLOWED_URL_SCHEMES),
        link_rel=None,
    )
    markdown = str(_MarkdownConverter(heading_style="ATX", bullets="-", escape_misc=False).convert(cleaned))
    markdown = _PLACEHOLDER_LIKE_RE.sub(r"{\\{", markdown)
    markdown = _EMPTY_IMAGE_RE.sub("", markdown)
    markdown = image_url_re.sub(lambda match: "{{attachment:" + match.group(1) + "}}", markdown)
    return _tidy(markdown)


def plain_to_markdown(text: str) -> str:
    """Return plain message text as Markdown that renders as the text reads."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        escaped = _INLINE_SPECIAL_RE.sub(r"\\\1", line)
        escaped = _LINE_START_RE.sub(_escape_line_start, escaped)
        # Leading spaces would make an indented code block: keep them visible.
        stripped = escaped.lstrip(" ")
        indent = len(escaped) - len(stripped)
        out.append("&nbsp;" * indent + stripped if indent else escaped)
    markdown = _join_hard_breaks(out)
    return _tidy(_PLACEHOLDER_LIKE_RE.sub(r"{\\{", markdown))


def _escape_line_start(match: re.Match[str]) -> str:
    marker = match.group(2)
    if marker[0].isdigit():
        return f"{match.group(1)}{marker[:-1]}\\{marker[-1]}"
    return f"{match.group(1)}\\{marker}"


def _join_hard_breaks(lines: list[str]) -> str:
    """Join lines, ending each line followed by another in the same paragraph with a hard break."""
    joined: list[str] = []
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line and following:
            joined.append(line + "\\")
        else:
            joined.append(line)
    return "\n".join(joined)


def _tidy(markdown: str) -> str:
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", markdown).strip()
