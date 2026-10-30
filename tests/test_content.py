"""Content as RT stores it: encodings, external storage, charsets."""

from __future__ import annotations

import base64
import hashlib
import quopri
from pathlib import Path

import pytest

from specivo_rt_migration.content import ContentUnavailableError, DiskStorage, decode_content, known_charset, to_text

THAI = "ภาษาไทย"


def test_none_takes_bytes_or_text() -> None:
    assert decode_content(b"\x00\xffraw", "none") == b"\x00\xffraw"
    assert decode_content(THAI, None) == THAI.encode()
    assert decode_content(None, "") == b""


def test_base64_and_quoted_printable() -> None:
    assert decode_content(base64.b64encode(b"\x89PNG"), "base64") == b"\x89PNG"
    assert decode_content(quopri.encodestring(THAI.encode()).decode(), "quoted-printable") == THAI.encode()


def test_unknown_encoding_is_reported() -> None:
    with pytest.raises(ContentUnavailableError, match="unknown content encoding"):
        decode_content(b"x", "x-uuencode")


def test_external_needs_a_storage_path() -> None:
    with pytest.raises(ContentUnavailableError) as raised:
        decode_content("a" * 64, "external")
    assert raised.value.external


def test_disk_storage_layout_and_hash(tmp_path: Path) -> None:
    data = b"stored outside the database"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / digest[:3] / digest[3:6] / digest[6:]
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    storage = DiskStorage(tmp_path)
    assert decode_content(digest, "external", storage) == data

    path.write_bytes(b"changed")
    with pytest.raises(ContentUnavailableError, match="does not hash"):
        decode_content(digest, "external", storage)
    with pytest.raises(ContentUnavailableError, match="not found"):
        decode_content("b" * 64, "external", storage)
    with pytest.raises(ContentUnavailableError, match="not a SHA-256"):
        decode_content("../../etc/passwd", "external", storage)


def test_utf8_first_then_the_declared_charset() -> None:
    # RT converts mail to UTF-8 and keeps the original charset in the headers.
    assert to_text(THAI.encode(), "TIS-620") == THAI
    # RT 4.4 could keep it as sent.
    assert to_text(THAI.encode("tis-620"), "TIS-620") == THAI
    assert to_text("Grüße".encode("iso-8859-1"), "iso-8859-1") == "Grüße"


def test_undecodable_bytes_never_stop_an_export() -> None:
    assert to_text(b"caf\xe9", None) == "caf�"
    assert to_text(b"caf\xe9", "x-unknown") == "caf�"
    assert to_text(b"\x81\x8d", "utf-16-nope") == "��"


def test_charset_names_mail_clients_use() -> None:
    assert known_charset("tis620") == "tis-620"
    assert known_charset('"UTF-8"') == "utf-8"
    assert known_charset("no-such-charset") is None
