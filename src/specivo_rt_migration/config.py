"""RT's settings the mapping depends on: lifecycles, priority names, the time zone.

RT 5.0 and later keep a setting changed in the web interface in the
``Configurations`` table; RT 4.4 keeps every setting in its configuration
files, which a database export cannot see. Where the database says nothing,
RT's own defaults apply, as they do in RT:

- **Lifecycles.** The ``default`` lifecycle: ``new`` (initial); ``open`` and
  ``stalled`` (active); ``resolved``, ``rejected`` and ``deleted`` (inactive).
  A status used by tickets that no known lifecycle names (a lifecycle defined
  in a configuration file) is placed by its name, and reported.
- **Priority names** (RT 5.0 and later): ``Low`` from 0, ``Medium`` from 50,
  ``High`` from 100. RT 4.4 shows priorities as numbers; the tool puts them in
  the same three buckets, and keeps every number in the issue's ``extra``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from specivo_migration_sdk import source_ref

logger = logging.getLogger(__name__)

# RT's lifecycle classes, and the Specivo category of each.
CATEGORY_FOR_CLASS: dict[str, str] = {"initial": "backlog", "active": "active", "inactive": "closed"}

DEFAULT_LIFECYCLE: dict[str, list[str]] = {
    "initial": ["new"],
    "active": ["open", "stalled"],
    "inactive": ["resolved", "rejected", "deleted"],
}

# Categories for status names no known lifecycle defines: RT's default
# statuses by name, anything else in progress.
_GUESSED_CATEGORIES: dict[str, str] = {
    "new": "backlog",
    "open": "active",
    "stalled": "active",
    "resolved": "closed",
    "rejected": "closed",
    "deleted": "closed",
}

# RT's default %PriorityAsString (RT 5.0 and later), also the buckets used for
# RT 4.4 and for queues that show numbers.
DEFAULT_PRIORITY_NAMES: tuple[tuple[str, int], ...] = (("Low", 0), ("Medium", 50), ("High", 100))


@dataclass
class Lifecycle:
    name: str
    # Status name (lower case) -> RT class: initial, active or inactive.
    classes: dict[str, str] = field(default_factory=dict)
    # Status names in the order the lifecycle lists them.
    order: list[str] = field(default_factory=list)
    on_create: str | None = None


def _lifecycle(name: str, definition: Mapping[str, Any]) -> Lifecycle:
    lifecycle = Lifecycle(name=name)
    for rt_class in ("initial", "active", "inactive"):
        for status in definition.get(rt_class) or []:
            key = str(status).lower()
            if key not in lifecycle.classes:
                lifecycle.classes[key] = rt_class
                lifecycle.order.append(key)
    defaults = definition.get("defaults")
    if isinstance(defaults, Mapping) and defaults.get("on_create"):
        lifecycle.on_create = str(defaults["on_create"]).lower()
    elif lifecycle.order:
        lifecycle.on_create = lifecycle.order[0]
    return lifecycle


@dataclass
class Lifecycles:
    """The ticket lifecycles RT has, by name, with ``default`` always present."""

    by_name: dict[str, Lifecycle]
    # Where they came from, for the report: "database" or "defaults".
    origin: str = "defaults"

    @classmethod
    def from_setting(cls, value: Any) -> Lifecycles:
        lifecycles = {"default": _lifecycle("default", DEFAULT_LIFECYCLE)}
        origin = "defaults"
        if isinstance(value, Mapping):
            origin = "database"
            for name, definition in value.items():
                if not isinstance(definition, Mapping):
                    continue
                # Asset lifecycles, and the internal map of which lifecycle can
                # move to which, are not ticket lifecycles.
                if str(definition.get("type") or "ticket") != "ticket" or str(name).startswith("__"):
                    continue
                lifecycles[str(name)] = _lifecycle(str(name), definition)
        return cls(by_name=lifecycles, origin=origin)

    def for_queue(self, lifecycle_name: str | None) -> Lifecycle:
        return self.by_name.get(lifecycle_name or "default") or self.by_name["default"]

    def category(self, lifecycle_name: str | None, status: str) -> tuple[str, bool]:
        """Specivo's category for *status* in a queue of *lifecycle_name*, and whether it was guessed."""
        key = status.lower()
        rt_class = self.for_queue(lifecycle_name).classes.get(key)
        if rt_class is None:
            for lifecycle in self.by_name.values():
                rt_class = lifecycle.classes.get(key)
                if rt_class is not None:
                    break
        if rt_class is not None:
            return CATEGORY_FOR_CLASS[rt_class], False
        return _GUESSED_CATEGORIES.get(key, "active"), key not in _GUESSED_CATEGORIES

    def is_inactive(self, lifecycle_name: str | None, status: str) -> bool:
        return self.category(lifecycle_name, status)[0] == "closed"


def status_ref(status: str) -> str:
    """The bundle ref of an RT status: its name in lower case (RT compares statuses case-insensitively)."""
    return source_ref(status.strip().lower() or "(none)")


@dataclass
class PriorityNames:
    """How RT names a ticket's numeric priority, per queue."""

    enabled: bool
    # Queue name or "Default" -> (label, lowest number) pairs; an empty list
    # means that queue shows numbers.
    mappings: dict[str, list[tuple[str, int]]]
    named: bool

    @classmethod
    def from_settings(cls, enable: Any, mapping: Any, *, supports_names: bool) -> PriorityNames:
        """Read ``$EnablePriorityAsString`` and ``%PriorityAsString`` (``None`` where unset)."""
        if not supports_names:
            return cls(enabled=False, mappings={}, named=False)
        enabled = True if enable is None else bool(_number(enable, 1))
        mappings: dict[str, list[tuple[str, int]]] = {"Default": list(DEFAULT_PRIORITY_NAMES)}
        if isinstance(mapping, Mapping):
            mappings = {str(queue): _pairs(value) for queue, value in mapping.items()}
            mappings.setdefault("Default", [])
        return cls(enabled=enabled, mappings=mappings, named=enabled)

    def mapping_for(self, queue_name: str) -> list[tuple[str, int]]:
        if not self.enabled:
            return []
        if queue_name in self.mappings:
            return self.mappings[queue_name]
        return self.mappings.get("Default", [])

    def label(self, queue_name: str, number: int | None) -> tuple[str, bool]:
        """The name RT shows for *number* in *queue_name*, and whether RT has that name.

        A queue whose priorities RT shows as numbers gets the default
        buckets. A number below the lowest named one takes the lowest name.
        """
        pairs = self.mapping_for(queue_name)
        named = bool(pairs)
        if not pairs:
            pairs = list(DEFAULT_PRIORITY_NAMES)
        value = number or 0
        ordered = sorted(pairs, key=lambda pair: pair[1], reverse=True)
        for name, lowest in ordered:
            if value >= lowest:
                return name, named
        return ordered[-1][0], named

    def all_labels(self) -> list[tuple[str, int]]:
        """Every label any queue uses, each with its lowest number, lowest first."""
        seen: dict[str, int] = {}
        for pairs in [*self.mappings.values(), list(DEFAULT_PRIORITY_NAMES)]:
            for name, number in pairs:
                key = name.lower()
                if key not in {existing.lower() for existing in seen}:
                    seen[name] = number
        return sorted(seen.items(), key=lambda item: (item[1], item[0].lower()))


def priority_ref(label: str) -> str:
    return source_ref(label.strip().lower() or "(none)")


def _number(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pairs(value: Any) -> list[tuple[str, int]]:
    """A queue's priority names: an ordered list of name, number; a hash; or 0 for numbers."""
    if isinstance(value, Mapping):
        return sorted(((str(name), _number(number, 0)) for name, number in value.items()), key=lambda pair: pair[1])
    if isinstance(value, Sequence) and not isinstance(value, str):
        items = list(value)
        return [(str(items[index]), _number(items[index + 1], 0)) for index in range(0, len(items) - 1, 2)]
    return []
