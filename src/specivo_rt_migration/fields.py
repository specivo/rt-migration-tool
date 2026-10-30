"""Ticket custom fields: their definitions, values and history.

RT's custom fields of tickets (lookup type ``RT::Queue-RT::Ticket``) become
issue custom fields, applied to the queues RT applies them to (or to every
project when RT applies one globally). Each RT type becomes a Specivo format:

==================================  =========================================
RT type                             Specivo field
==================================  =========================================
Select                              ``list``, its values as the choices
Freeform, Combobox, Autocomplete    ``string`` (suggested values in ``extra``)
Text, Wikitext                      ``text``, as written
HTML (RT 6.0)                       ``text``, converted to Markdown
Date                                ``date`` (``YYYY-MM-DD``)
DateTime                            ``string``, an ISO 8601 time in UTC
IPAddress, IPAddressRange           ``string``, written as RT shows it
Image, Binary                       no field: each value is a file on the issue
==================================  =========================================

A field that takes more than one value (``MaxValues`` other than 1) is
``multiple``, its values a list. Its metadata key comes from its name (letters,
digits and ``_``), or ``cf_<id>`` for a name in another script; ``--cf-key-map``
overrides it.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from specivo_migration_sdk import CustomField, source_ref

from specivo_rt_migration.content import ContentUnavailableError, DiskStorage, decode_content, to_text
from specivo_rt_migration.markdown import html_to_markdown

TICKET_LOOKUP = "RT::Queue-RT::Ticket"
FILE_TYPES = frozenset({"Image", "Binary"})
_KEY_RE = re.compile(r"[^A-Za-z0-9_]+")
_PADDED_IPV4_RE = re.compile(r"^\s*(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\s*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class FieldInfo:
    """One ticket custom field as the export carries it."""

    id: int
    name: str
    rt_type: str
    multiple: bool
    ref: str
    key: str
    choices: list[str] = field(default_factory=list)

    @property
    def is_file(self) -> bool:
        return self.rt_type in FILE_TYPES

    @property
    def field_format(self) -> str:
        return {
            "Select": "list",
            "Text": "text",
            "Wikitext": "text",
            "HTML": "text",
            "Date": "date",
        }.get(self.rt_type, "string")

    def json_schema(self) -> dict[str, Any]:
        item: dict[str, Any] = {"type": "string"}
        if self.rt_type == "Select" and self.choices:
            item["enum"] = list(self.choices)
        elif self.rt_type == "Date":
            item["format"] = "date"
        elif self.rt_type == "DateTime":
            item["format"] = "date-time"
        return {"type": "array", "items": item} if self.multiple else item


def field_key(name: str, field_id: int, taken: set[str], override: str | None = None) -> str:
    """A metadata key for a field: from *override*, else from its name; unique among *taken*."""
    if override:
        key = override
    else:
        key = _KEY_RE.sub("_", name).strip("_").lower()
        if not key or not re.match(r"[a-z_]", key) or not re.search(r"[a-z]", key):
            key = f"cf_{field_id}"
    if key in taken:
        key = f"{key}_{field_id}"
    taken.add(key)
    return key


def build_fields(
    rows: Sequence[Mapping[str, Any]],
    *,
    choices: Mapping[int, list[str]],
    key_overrides: Mapping[str, str],
) -> dict[int, FieldInfo]:
    """The ticket custom fields of RT, by id (disabled ones too: their values are history)."""
    taken: set[str] = set()
    fields: dict[int, FieldInfo] = {}
    for row in sorted(rows, key=lambda row: (int(row.get("sortorder") or 0), int(row["id"]))):
        if row.get("lookuptype") != TICKET_LOOKUP:
            continue
        field_id = int(row["id"])
        name = str(row.get("name") or f"Custom field {field_id}")
        fields[field_id] = FieldInfo(
            id=field_id,
            name=name,
            rt_type=str(row.get("type") or "Freeform"),
            multiple=int(row.get("maxvalues") or 0) != 1,
            ref=source_ref(field_id),
            key=field_key(name, field_id, taken, key_overrides.get(name)),
            choices=list(choices.get(field_id, [])),
        )
    return fields


def definition(
    info: FieldInfo, row: Mapping[str, Any], *, is_for_all: bool, project_refs: list[str], suggestions: list[str]
) -> CustomField:
    extra: dict[str, Any] = {"rt_custom_field": info.id, "rt_type": info.rt_type}
    if row.get("description"):
        extra["description"] = row["description"]
    if row.get("pattern"):
        extra["pattern"] = row["pattern"]
    if suggestions and info.rt_type != "Select":
        extra["suggested_values"] = suggestions
    if int(row.get("disabled") or 0):
        extra["disabled"] = True
    return CustomField(
        source_ref=info.ref,
        name=info.name,
        key=info.key,
        field_format=info.field_format,
        json_schema=info.json_schema(),
        multiple=info.multiple,
        is_for_all=is_for_all,
        project_refs=[] if is_for_all else project_refs,
        extra=extra,
    )


def value_text(info: FieldInfo, row: Mapping[str, Any], storage: DiskStorage | None) -> str:
    """One value of a field as Specivo keeps it (not for file fields).

    Raises :class:`ContentUnavailableError` when a long value is in external
    storage the tool cannot read.
    """
    content = str(row.get("content") or "")
    large = row.get("largecontent")
    if large is not None and (len(large) > 0):
        data = decode_content(large, row.get("contentencoding"), storage)
        long_text = to_text(data, None)
    else:
        long_text = None

    if info.rt_type == "IPAddress":
        return _ip(content)
    if info.rt_type == "IPAddressRange":
        start = _ip(content)
        end = _ip(long_text or "") if long_text else start
        return start if end == start else f"{start}-{end}"
    text = long_text if long_text is not None else content
    if info.rt_type == "HTML" or (row.get("contenttype") or "").lower() == "text/html":
        return html_to_markdown(text, lambda _ref: None)
    if info.rt_type == "DateTime":
        return _datetime(text)
    if info.rt_type == "Date":
        return text.strip()[:10] if _DATE_RE.match(text.strip()[:10]) else text
    return text


def scalar(info: FieldInfo, texts: list[str]) -> Any:
    """The value of a field on an issue, from its current values in order."""
    if info.multiple:
        return texts
    return texts[0] if texts else None


def _ip(value: str) -> str:
    """An address as RT shows it: RT stores IPv4 zero-padded (``192.000.002.010``) for sorting."""
    match = _PADDED_IPV4_RE.match(value)
    if match:
        return ".".join(str(int(part)) for part in match.groups())
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError:
        return value.strip()


def _datetime(value: str) -> str:
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.isoformat()


def file_value(row: Mapping[str, Any], storage: DiskStorage | None) -> tuple[str, bytes, str | None]:
    """An Image or Binary value: its file name, bytes and type. Raises :class:`ContentUnavailableError`."""
    filename = str(row.get("content") or "").strip() or f"custom-field-value-{row['id']}"
    data = decode_content(row.get("largecontent"), row.get("contentencoding"), storage)
    return filename, data, (row.get("contenttype") or None)


__all__ = ["ContentUnavailableError", "FieldInfo", "build_fields", "definition", "file_value", "scalar", "value_text"]
