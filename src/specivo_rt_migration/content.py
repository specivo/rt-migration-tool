"""The bytes RT keeps for an attachment or a custom field value, and their text.

RT stores content in ``Attachments.Content`` and
``ObjectCustomFieldValues.LargeContent`` with a ``ContentEncoding`` saying how:

- ``none`` (or empty): as it is. On MySQL the column is a BLOB and holds the
  bytes; on PostgreSQL it is ``text``, which only ever holds valid UTF-8 this
  way;
- ``base64`` and ``quoted-printable``: encoded, which RT does on PostgreSQL for
  anything that is not valid UTF-8;
- ``external``: moved out of the database by ``rt-externalize-attachments``.
  The column then holds the SHA-256 of the content, which is also its key in
  the storage backend. The Disk backend keeps it at
  ``<root>/<first 3 hex digits>/<next 3>/<the rest>``; the tool reads that
  backend when given its root (``--external-storage-path``). Content in any
  other backend (Amazon S3, Dropbox) cannot be read from here, and is counted.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import quopri
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ContentUnavailableError(Exception):
    """The content cannot be read: in external storage the tool was not given, or missing there."""

    def __init__(self, reason: str, *, external: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.external = external


@dataclass(frozen=True)
class DiskStorage:
    """RT's Disk external storage backend, at its root directory."""

    root: Path

    def path_for(self, digest: str) -> Path:
        key = digest.strip().lower()
        if not _SHA256_RE.fullmatch(key):
            raise ContentUnavailableError(f"external storage key {digest[:80]!r} is not a SHA-256", external=True)
        return self.root / key[:3] / key[3:6] / key[6:]

    def read(self, digest: str) -> bytes:
        path = self.path_for(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ContentUnavailableError(f"not found in external storage ({path})", external=True) from None
        except OSError as exc:
            raise ContentUnavailableError(f"cannot read {path}: {exc.strerror or exc}", external=True) from None
        if hashlib.sha256(data).hexdigest() != digest.strip().lower():
            raise ContentUnavailableError(f"{path} does not hash to its name", external=True)
        return data


def decode_content(content: str | bytes | None, encoding: str | None, storage: DiskStorage | None = None) -> bytes:
    """Return the bytes RT stored, undoing its ``ContentEncoding``.

    Raises :class:`ContentUnavailableError` for external content the tool cannot
    read, and for an encoding RT does not write.
    """
    raw = _as_bytes(content)
    kind = (encoding or "none").strip().lower()
    if kind in ("none", ""):
        return raw
    if kind == "base64":
        try:
            return base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            raise ContentUnavailableError("base64 content that does not decode") from None
    if kind == "quoted-printable":
        return quopri.decodestring(raw)
    if kind == "external":
        digest = raw.decode("ascii", errors="replace")
        if storage is None:
            raise ContentUnavailableError(
                "in RT's external storage, and no --external-storage-path was given", external=True
            )
        return storage.read(digest)
    raise ContentUnavailableError(f"unknown content encoding {encoding!r}")


def _as_bytes(content: str | bytes | None) -> bytes:
    if content is None:
        return b""
    if isinstance(content, str):
        return content.encode("utf-8", errors="surrogateescape")
    return bytes(content)


def known_charset(charset: str | None) -> str | None:
    """Return Python's name for *charset*, or ``None`` when Python does not know it."""
    if not charset:
        return None
    name = charset.strip().strip("\"'").lower()
    # Names mail clients use that Python spells differently.
    name = {"tis620": "tis-620", "windows-874": "cp874", "x-sjis": "shift_jis", "ks_c_5601-1987": "cp949"}.get(
        name, name
    )
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def to_text(data: bytes, charset: str | None) -> str:
    """Return *data* as text, honouring the part's declared *charset*.

    RT converts the text of incoming mail to UTF-8 before storing it but
    leaves the original charset in the part's headers, so UTF-8 is tried
    first whenever it decodes cleanly; then the declared charset; then
    Windows-1252, which decodes anything. Malformed bytes never stop an
    export: they become U+FFFD.
    """
    declared = known_charset(charset)
    if declared in (None, "utf-8", "ascii"):
        return data.decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(declared)
    except (UnicodeDecodeError, LookupError):
        return data.decode("cp1252", errors="replace")
