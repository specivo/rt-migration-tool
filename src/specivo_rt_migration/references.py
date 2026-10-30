"""References to other tickets in text, turned into issue placeholders.

RT links nothing in message text by itself, so people write ticket numbers in
a few set ways. The tool links only those, to keep numbers that mean
something else ("step 2", "#3 on the list", a colour) as they are:

- RT's subject tag, ``[example.com #123]``: a bracket holding one word and
  ``#`` with the number;
- ``RT #123``, ``RT#123`` and ``RT 123`` (``RT`` in capitals);
- ``ticket #123``, ``ticket 123``, ``Ticket #123`` (any case);
- a bare ``#123``, only with ``--link-hash-numbers``.

A number is linked only when RT has that ticket. A ticket in the bundle (or
merged into one) becomes ``{{issue:REF}}``, which Specivo shows as the
issue's new key; a ticket RT has that this bundle does not (another queue,
exported in a bundle of its own) becomes ``{{issue:REF|text}}``, which links
once that bundle is loaded and otherwise reads as the text did. Text in code
blocks, code spans, links and URLs is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from specivo_migration_sdk import placeholders

# Stretches never touched: fenced code blocks, code spans, Markdown links and
# images, autolinks, bare URLs, and placeholders already written.
_PROTECTED_RE = re.compile(
    r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)"
    r"|`+[^`\n]*`+"
    r"|!?\[[^\]\n]*\]\([^)\n]*\)"
    r"|<[a-z][a-z0-9+.-]*:[^>\s]*>"
    r"|\b(?:https?|ftp|mailto):[^\s<>()\]]+"
    r"|\{\{[^{}\n]*\}\}"
)
_TOKEN = "ZZSPVREF{index}ZZ"
_TOKEN_RE = re.compile(r"ZZSPVREF(\d+)ZZ")

_SUBJECT_TAG = r"\\?\[[^\]\s#\[\\]+ \\?#(?P<tag>\d+)\\?\]"
_RT = r"(?<![\w#])RT(?: ?\\?# ?| )(?P<rt>\d+)\b"
_TICKET = r"(?<![\w#])(?i:ticket)(?: ?\\?# ?| )(?P<ticket>\d+)\b"
_HASH = r"(?<![\w&\\#/])#(?P<hash>\d+)\b"


@dataclass(frozen=True)
class TicketIndex:
    """What a ticket number names: the surviving ticket's ref, and whether it is in the bundle."""

    resolve: Callable[[int], tuple[str, bool] | None]


def link_references(markdown: str, index: TicketIndex, *, hash_numbers: bool = False) -> str:
    """Return *markdown* with references to tickets RT has replaced by issue placeholders."""
    if not markdown or not any(char.isdigit() for char in markdown):
        return markdown
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return _TOKEN.format(index=len(protected) - 1)

    text = _PROTECTED_RE.sub(protect, markdown)
    pattern = re.compile("|".join([_SUBJECT_TAG, _RT, _TICKET, *([_HASH] if hash_numbers else [])]))

    def on_reference(match: re.Match[str]) -> str:
        number = next(value for value in match.groupdict().values() if value)
        found = index.resolve(int(number))
        if found is None:
            return match.group(0)
        ref, in_bundle = found
        label = _label(match.group(0))
        if in_bundle:
            return placeholders.issue(ref)
        return placeholders.issue(ref, label)

    text = pattern.sub(on_reference, text)
    return _TOKEN_RE.sub(lambda match: protected[int(match.group(1))], text)


def _label(text: str) -> str:
    """The reference as the reader saw it, without Markdown escapes, safe as a placeholder label."""
    label = text.replace("\\#", "#").replace("\\[", "[").replace("\\]", "]")
    return re.sub(r"[{}\n|]|\]\]", " ", label).strip() or text
