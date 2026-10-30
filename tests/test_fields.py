"""Custom fields of each RT type: definitions and values."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from specivo_rt_migration.fields import FieldInfo, build_fields, field_key, file_value, value_text


def _field(rt_type: str, *, multiple: bool = False, choices: list[str] | None = None) -> FieldInfo:
    return FieldInfo(id=9, name=rt_type, rt_type=rt_type, multiple=multiple, ref="9", key="k", choices=choices or [])


def test_keys() -> None:
    taken: set[str] = set()
    assert field_key("Customer reference", 4, taken) == "customer_reference"
    assert field_key("Customer-Reference", 5, taken) == "customer_reference_5"
    assert field_key("ลูกค้า", 15, taken) == "cf_15"
    assert field_key("2nd line", 16, taken) == "cf_16"
    assert field_key("Anything", 17, taken, "chosen") == "chosen"


@pytest.mark.parametrize(
    ("rt_type", "multiple", "field_format", "schema"),
    [
        ("Select", False, "list", {"type": "string", "enum": ["A", "B"]}),
        ("Select", True, "list", {"type": "array", "items": {"type": "string", "enum": ["A", "B"]}}),
        ("Freeform", True, "string", {"type": "array", "items": {"type": "string"}}),
        ("Combobox", False, "string", {"type": "string"}),
        ("Autocomplete", False, "string", {"type": "string"}),
        ("Text", False, "text", {"type": "string"}),
        ("Wikitext", False, "text", {"type": "string"}),
        ("HTML", False, "text", {"type": "string"}),
        ("Date", False, "date", {"type": "string", "format": "date"}),
        ("DateTime", False, "string", {"type": "string", "format": "date-time"}),
        ("IPAddress", False, "string", {"type": "string"}),
        ("IPAddressRange", False, "string", {"type": "string"}),
    ],
)
def test_formats(rt_type: str, multiple: bool, field_format: str, schema: dict[str, Any]) -> None:
    info = _field(rt_type, multiple=multiple, choices=["A", "B"])
    assert info.field_format == field_format
    assert info.json_schema() == schema
    assert not info.is_file


def test_file_types() -> None:
    assert _field("Image").is_file and _field("Binary").is_file


@pytest.mark.parametrize(
    ("rt_type", "row", "expected"),
    [
        ("Freeform", {"content": "plain"}, "plain"),
        ("Text", {"content": "", "largecontent": "long\ntext", "contentencoding": "none"}, "long\ntext"),
        ("Text", {"content": "", "largecontent": b"bytes on MySQL", "contentencoding": None}, "bytes on MySQL"),
        ("HTML", {"content": "<p><b>bold</b></p>"}, "**bold**"),
        ("Date", {"content": "2026-12-01"}, "2026-12-01"),
        ("DateTime", {"content": "2026-12-01 15:00:00"}, "2026-12-01T15:00:00Z"),
        ("IPAddress", {"content": "192.000.002.010"}, "192.0.2.10"),
        ("IPAddress", {"content": "2001:0db8:0000:0000:0000:0000:0000:0001"}, "2001:db8::1"),
        (
            "IPAddressRange",
            {"content": "198.051.100.000", "largecontent": "198.051.100.255"},
            "198.51.100.0-198.51.100.255",
        ),
        ("IPAddressRange", {"content": "203.000.113.005", "largecontent": "203.000.113.005"}, "203.0.113.5"),
    ],
)
def test_values(rt_type: str, row: dict[str, Any], expected: str) -> None:
    assert value_text(_field(rt_type), {"id": 1, **row}, None) == expected


def test_file_values() -> None:
    row = {"id": 3, "content": "shot.png", "largecontent": base64.b64encode(b"\x89PNG"), "contentencoding": "base64",
           "contenttype": "image/png"}  # fmt: skip
    assert file_value(row, None) == ("shot.png", b"\x89PNG", "image/png")
    assert file_value({"id": 4, "content": "", "largecontent": b"x"}, None)[0] == "custom-field-value-4"


def test_build_fields_keeps_only_ticket_fields() -> None:
    rows = [
        {"id": 1, "name": "Content", "type": "HTML", "maxvalues": 1, "lookuptype": "RT::Class-RT::Article"},
        {"id": 2, "name": "Severity", "type": "Select", "maxvalues": 1, "lookuptype": "RT::Queue-RT::Ticket"},
        {"id": 3, "name": "Tags", "type": "Freeform", "maxvalues": 0, "lookuptype": "RT::Queue-RT::Ticket"},
        {"id": 4, "name": "Cost", "type": "Freeform", "maxvalues": 1, "lookuptype": "RT::Queue"},
    ]
    fields = build_fields(rows, choices={2: ["Low"]}, key_overrides={"Tags": "labels"})
    assert set(fields) == {2, 3}
    assert fields[2].choices == ["Low"] and not fields[2].multiple
    assert fields[3].multiple and fields[3].key == "labels"
