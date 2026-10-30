"""Messages taken apart: which part is the body, which parts are files."""

from __future__ import annotations

import base64
import quopri
from typing import Any

from specivo_rt_migration.mime import build_tree, decoded_header, referenced_content_ids, take_apart


def _row(part_id: int, parent: int, content_type: str, content: Any = "", **values: Any) -> dict[str, Any]:
    return {
        "id": part_id,
        "transactionid": 1,
        "parent": parent,
        "contenttype": content_type,
        "content": content,
        **values,
    }


def _message(*rows: dict[str, Any]) -> Any:
    tops = build_tree(rows)
    return take_apart(tops[0], None)


def test_single_plain_part() -> None:
    message = _message(_row(1, 0, "text/plain", "Hello\n", headers="Content-Type: text/plain; charset=utf-8\n"))
    assert message.body is not None
    assert message.body.text == "Hello\n"
    assert message.body.html is False
    assert message.files == []


def test_alternative_prefers_html() -> None:
    message = _message(
        _row(1, 0, "multipart/alternative"),
        _row(2, 1, "text/plain", "plain"),
        _row(3, 1, "text/html", "<p>rich</p>"),
    )
    assert message.body is not None
    assert message.body.html is True
    assert message.body.text == "<p>rich</p>"


def test_nested_related_with_inline_image_and_attachment() -> None:
    message = _message(
        _row(1, 0, "multipart/mixed"),
        _row(2, 1, "multipart/alternative"),
        _row(3, 2, "text/plain", "plain"),
        _row(4, 2, "multipart/related"),
        _row(5, 4, "text/html", '<img src="cid:img1@example.com">'),
        _row(
            6,
            4,
            "image/png",
            base64.b64encode(b"\x89PNG").decode(),
            contentencoding="base64",
            filename="shot.png",
            headers="Content-ID: <img1@example.com>\nContent-Disposition: inline\n",
        ),
        _row(7, 1, "application/pdf", "%PDF", filename="a.pdf"),
    )
    assert message.body is not None and message.body.html
    assert [part.id for part in message.files] == [6, 7]
    assert message.content_ids == {"img1@example.com": message.files[0]}
    assert message.files[0].data(None) == b"\x89PNG"
    assert referenced_content_ids(message.body.text) == {"img1@example.com"}


def test_text_attachment_with_a_name_is_a_file_and_later_text_joins_the_body() -> None:
    message = _message(
        _row(1, 0, "multipart/mixed"),
        _row(2, 1, "text/plain", "First"),
        _row(3, 1, "text/plain", "log", filename="server.log"),
        _row(4, 1, "text/plain", "-- list footer"),
        _row(5, 1, "text/plain", "inline but attached", headers="Content-Disposition: attachment\n"),
    )
    assert message.body is not None
    assert message.body.text == "First\n\n-- list footer"
    assert [part.id for part in message.files] == [3, 5]
    assert message.files[1].display_filename() == "part-5.txt"


def test_attached_message_parts_are_files() -> None:
    message = _message(
        _row(1, 0, "multipart/mixed"),
        _row(2, 1, "text/plain", "See the forwarded mail."),
        _row(3, 1, "message/rfc822", ""),
        _row(4, 3, "text/plain", "Original text"),
    )
    assert message.body is not None and message.body.text == "See the forwarded mail."
    assert [part.id for part in message.files] == [4]


def test_quoted_printable_in_a_declared_charset() -> None:
    thai = "สวัสดี".encode("tis-620")
    message = _message(
        _row(
            1,
            0,
            "text/plain",
            quopri.encodestring(thai).decode(),
            contentencoding="quoted-printable",
            headers="Content-Type: text/plain; charset=TIS-620\n",
        )
    )
    assert message.body is not None and message.body.text == "สวัสดี"


def test_mixed_plain_and_html_text_stays_readable() -> None:
    message = _message(
        _row(1, 0, "multipart/mixed"),
        _row(2, 1, "text/html", "<p>rich</p>"),
        _row(3, 1, "text/plain", "a < b"),
    )
    assert message.body is not None
    assert message.body.text == "<p>rich</p>\n\n<pre>a &lt; b</pre>"


def test_empty_parts_without_a_name_are_skipped() -> None:
    message = _message(_row(1, 0, "multipart/mixed"), _row(2, 1, "text/plain", ""), _row(3, 1, "image/png", ""))
    assert message.body is None
    assert message.files == []


def test_mail_headers() -> None:
    headers = (
        "Subject: =?UTF-8?B?4Lig4Liy4Lip4Liy4LmE4LiX4Lii?=\n"
        "From: Dana Customer <dana@example.net>\n"
        "To: support@example.com, other@example.com\n"
        "Cc: =?UTF-8?Q?Ev=C3=A9?= <eve@example.org>\n"
        "Message-ID: <abc@example.net>\n"
        "RT-Send-CC: carol@example.com\n"
    )
    message = _message(_row(1, 0, "text/plain", "x", headers=headers))
    assert message.mail_headers() == {
        "message_id": "abc@example.net",
        "from": ["Dana Customer <dana@example.net>"],
        "to": ["support@example.com", "other@example.com"],
        "cc": ["Evé <eve@example.org>"],
        "rt_send_cc": ["carol@example.com"],
        "subject": "ภาษาไทย",
    }


def test_decoded_header_keeps_undecodable_text() -> None:
    assert decoded_header("=?x-unknown?B?AAAA?=") == "=?x-unknown?B?AAAA?="
