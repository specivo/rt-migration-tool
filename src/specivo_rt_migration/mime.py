"""A transaction's message: the MIME tree RT stored, its body and its files.

RT keeps each message it records (a reply, a comment, the message a ticket
was created with) as rows of ``Attachments``: one per MIME part, linked to
their parent part by ``Parent``, the top part with ``Parent = 0``. A part's
headers are kept as text; its content is decoded by
:mod:`specivo_rt_migration.content`.

The body is chosen the way a reader of RT sees the message:

- a text part with no file name, not marked ``Content-Disposition:
  attachment``, is message text. The first one is the body; the text of later
  ones (a list footer, a second part of a split message) is appended to it;
- of the parts of a ``multipart/alternative``, the HTML one is taken, which
  keeps the formatting, else the plain text;
- every other leaf part with content (a file, an image, a forwarded message's
  parts) is one of the message's files. An image referred to from the HTML
  body by its ``Content-ID`` (``<img src="cid:...">``) is a file too, and the
  body shows it in place.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.headerregistry import Address
from email.message import Message
from email.parser import HeaderParser
from email.utils import getaddresses
from typing import Any

from specivo_rt_migration.content import ContentUnavailableError, DiskStorage, decode_content, to_text

_CID_RE = re.compile(r"""cid:([^"'\s)>]+)""", re.IGNORECASE)


@dataclass
class Part:
    """One row of ``Attachments``: a MIME part of a recorded message."""

    id: int
    transaction_id: int
    parent: int
    content_type: str
    filename: str | None = None
    message_id: str | None = None
    subject: str | None = None
    content_encoding: str | None = None
    content: str | bytes | None = None
    headers: str | None = None
    creator: int | None = None
    created: Any = None
    children: list[Part] = field(default_factory=list)
    _parsed: Message | None = field(default=None, repr=False)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Part:
        return cls(
            id=int(row["id"]),
            transaction_id=int(row["transactionid"] or 0),
            parent=int(row["parent"] or 0),
            content_type=(row.get("contenttype") or "").strip().lower() or "application/octet-stream",
            filename=(row.get("filename") or None),
            message_id=row.get("messageid") or None,
            subject=row.get("subject") or None,
            content_encoding=row.get("contentencoding"),
            content=row.get("content"),
            headers=row.get("headers"),
            creator=row.get("creator"),
            created=row.get("created"),
        )

    # -- headers ---------------------------------------------------------

    @property
    def header(self) -> Message:
        if self._parsed is None:
            self._parsed = HeaderParser().parsestr((self.headers or "").replace("\r\n", "\n"))
        return self._parsed

    def header_value(self, name: str) -> str | None:
        value = self.header.get(name)
        return None if value is None else " ".join(str(value).split())

    @property
    def charset(self) -> str | None:
        return self.header.get_content_charset()

    @property
    def content_id(self) -> str | None:
        value = self.header_value("Content-ID")
        return value.strip().strip("<>").strip() if value else None

    @property
    def disposition(self) -> str:
        value = self.header_value("Content-Disposition") or ""
        return value.split(";", 1)[0].strip().lower()

    # -- shape -----------------------------------------------------------

    @property
    def is_multipart(self) -> bool:
        return self.content_type.startswith("multipart/")

    @property
    def is_text(self) -> bool:
        return self.content_type in ("text/plain", "text/html")

    @property
    def is_message_text(self) -> bool:
        """Text a reader sees as the message rather than as an attached file."""
        return self.is_text and not self.filename and self.disposition != "attachment"

    def data(self, storage: DiskStorage | None) -> bytes:
        return decode_content(self.content, self.content_encoding, storage)

    def text(self, storage: DiskStorage | None) -> str:
        return to_text(self.data(storage), self.charset)

    def display_filename(self) -> str:
        """The part's file name, or one made up from its id and type for a part without."""
        if self.filename and self.filename.strip():
            return " ".join(self.filename.split())[:255]
        extension = mimetypes.guess_extension(self.content_type) or ".bin"
        if self.content_type == "message/rfc822":
            extension = ".eml"
        return f"part-{self.id}{extension}"


@dataclass
class Body:
    """The text of a message: HTML or plain text, and where it came from."""

    text: str
    html: bool
    part_ids: list[int] = field(default_factory=list)


@dataclass
class MailMessage:
    """A transaction's message, taken apart."""

    top: Part
    body: Body | None
    files: list[Part]
    # Text parts that could not be read (external storage the tool was not given).
    unreadable: list[tuple[Part, ContentUnavailableError]] = field(default_factory=list)

    @property
    def content_ids(self) -> dict[str, Part]:
        return {part.content_id: part for part in self.files if part.content_id}

    def mail_headers(self) -> dict[str, Any]:
        """What the message said about itself and its recipients, for ``extra``."""
        headers: dict[str, Any] = {}
        message_id = self.top.message_id or self.top.header_value("Message-ID")
        if message_id:
            headers["message_id"] = message_id.strip().strip("<>")
        for name, key in (
            ("From", "from"),
            ("To", "to"),
            ("Cc", "cc"),
            ("Bcc", "bcc"),
            ("RT-Send-CC", "rt_send_cc"),
            ("RT-Send-BCC", "rt_send_bcc"),
            ("Reply-To", "reply_to"),
        ):
            addresses = _addresses(self.top.header.get_all(name) or [])
            if addresses:
                headers[key] = addresses
        subject = self.top.subject or self.top.header_value("Subject")
        if subject:
            headers["subject"] = decoded_header(subject)
        return headers


def decoded_header(value: str) -> str:
    """A header value with its RFC 2047 encoded words decoded."""
    try:
        return " ".join(str(make_header(decode_header(value))).split())
    except (ValueError, LookupError):
        return value


def _addresses(values: Iterable[Any]) -> list[str]:
    found: list[str] = []
    for name, address in getaddresses([decoded_header(str(value)) for value in values]):
        if not address:
            continue
        found.append(str(Address(display_name=name, addr_spec=address)) if name else address)
    return found


def build_tree(rows: Iterable[Mapping[str, Any]]) -> list[Part]:
    """Return the top parts of the rows of one transaction, each with its children, in id order."""
    parts = sorted((Part.from_row(row) for row in rows), key=lambda part: part.id)
    by_id = {part.id: part for part in parts}
    tops: list[Part] = []
    for part in parts:
        parent = by_id.get(part.parent) if part.parent else None
        if parent is None:
            tops.append(part)
        else:
            parent.children.append(part)
    return tops


def take_apart(top: Part, storage: DiskStorage | None) -> MailMessage:
    """Find the body and the files of the message under *top*."""
    texts: list[Part] = []
    files: list[Part] = []
    _walk(top, texts, files, inside_message=False)
    message = MailMessage(top=top, body=None, files=files)
    chunks: list[str] = []
    html = any(part.content_type == "text/html" for part in texts)
    used: list[int] = []
    for part in texts:
        try:
            text = part.text(storage)
        except ContentUnavailableError as exc:
            message.unreadable.append((part, exc))
            continue
        if not text.strip():
            continue
        if html and part.content_type == "text/plain":
            # Mixed text: keep plain text plain inside an HTML body.
            text = "<pre>" + _escape_html(text) + "</pre>"
        chunks.append(text)
        used.append(part.id)
    if chunks:
        message.body = Body(text="\n\n".join(chunks), html=html, part_ids=used)
    return message


def _walk(part: Part, texts: list[Part], files: list[Part], *, inside_message: bool) -> None:
    if part.is_multipart:
        if part.content_type == "multipart/alternative" and not inside_message:
            chosen = _alternative(part)
            for child in part.children:
                if child is chosen:
                    _walk(child, texts, files, inside_message=inside_message)
                elif _has_files(child):
                    # An alternative that holds files of its own (an HTML
                    # version with its images): keep the files, not the text.
                    _collect_files(child, files)
            return
        for child in part.children:
            _walk(child, texts, files, inside_message=inside_message)
        return
    if part.content_type == "message/rfc822" and part.children:
        # RT parses an attached message into parts of its own: they are files.
        for child in part.children:
            _walk(child, texts, files, inside_message=True)
        return
    if part.is_message_text and not inside_message:
        texts.append(part)
        return
    empty = part.content is None or len(part.content) == 0
    if empty and not part.filename:
        return
    files.append(part)


def _alternative(part: Part) -> Part | None:
    """The alternative to show: HTML (directly or inside multipart/related), else plain text."""
    for wanted in ("text/html", "text/plain"):
        for child in part.children:
            if child.content_type == wanted and child.is_message_text:
                return child
            if child.is_multipart and any(grand.content_type == wanted for grand in child.children):
                return child
    return part.children[0] if part.children else None


def _has_files(part: Part) -> bool:
    if not part.is_multipart:
        return not part.is_message_text
    return any(_has_files(child) for child in part.children)


def _collect_files(part: Part, files: list[Part]) -> None:
    if part.is_multipart:
        for child in part.children:
            _collect_files(child, files)
    elif not part.is_message_text and part.content is not None:
        files.append(part)


def referenced_content_ids(html: str) -> set[str]:
    """The ``cid:`` targets an HTML body refers to."""
    return {match.group(1) for match in _CID_RE.finditer(html)}


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
