"""RT's settings: Data::Dumper values, lifecycles, priority names, version detection."""

from __future__ import annotations

import pytest

from specivo_rt_migration.config import Lifecycles, PriorityNames
from specivo_rt_migration.perl import PerlDataError, parse_dumper, parse_setting
from specivo_rt_migration.schema import UnsupportedSchemaError, classify

# -- Data::Dumper -------------------------------------------------------------


def test_dumper_values() -> None:
    text = r"""$VAR1 = {
  'default' => {
                 'active' => [ 'open', 'stalled' ],
                 'note' => 'it\'s \\ fine',
                 'count' => 3,
                 'ratio' => '0.5',
                 'off' => undef,
                 bare => -1.5e2,
                 'thai' => "\x{e20}\x{e32}\n"
               }
};"""
    assert parse_dumper(text) == {
        "default": {
            "active": ["open", "stalled"],
            "note": "it's \\ fine",
            "count": 3,
            "ratio": "0.5",
            "off": None,
            "bare": -150.0,
            "thai": "ภา\n",
        }
    }


def test_dumper_refuses_code() -> None:
    with pytest.raises(PerlDataError):
        parse_dumper("{ 'a' => sub { 1 } }")
    with pytest.raises(PerlDataError):
        parse_dumper("{ 'a' => 1")


def test_setting_by_content_type() -> None:
    assert parse_setting('{"a": [1]}', "application/json") == {"a": [1]}
    assert parse_setting(b"[ 'x' ]", "perl") == ["x"]
    assert parse_setting("plain", "") == "plain"
    assert parse_setting(None, "perl") is None


# -- lifecycles -----------------------------------------------------------------


def test_lifecycles_from_the_database() -> None:
    lifecycles = Lifecycles.from_setting(
        {
            "billing": {
                "initial": ["Quoted"],
                "active": ["invoiced"],
                "inactive": ["paid"],
                "defaults": {"on_create": "quoted"},
            },
            "assets": {"type": "asset", "initial": ["new"], "active": ["in-use"], "inactive": ["recycled"]},
            "__maps__": {},
        }
    )
    assert set(lifecycles.by_name) == {"default", "billing"}
    assert lifecycles.category("billing", "Quoted") == ("backlog", False)
    assert lifecycles.category("billing", "paid") == ("closed", False)
    # A status of another lifecycle (a ticket moved between queues) still has its class.
    assert lifecycles.category("default", "invoiced") == ("active", False)
    assert lifecycles.for_queue("missing").name == "default"
    assert lifecycles.by_name["billing"].on_create == "quoted"


def test_unknown_statuses_are_guessed_by_name() -> None:
    lifecycles = Lifecycles.from_setting(None)
    assert lifecycles.origin == "defaults"
    assert lifecycles.category(None, "resolved") == ("closed", False)
    assert lifecycles.category(None, "waiting-for-customer") == ("active", True)
    assert lifecycles.is_inactive(None, "rejected")


# -- priority names -------------------------------------------------------------


def test_priority_names_per_queue() -> None:
    names = PriorityNames.from_settings(
        None,
        {"Default": {"Low": 0, "Medium": 50, "High": 100}, "Billing": ["Routine", 0, "Urgent", 70], "Ops": 0},
        supports_names=True,
    )
    assert names.label("Support", 0) == ("Low", True)
    assert names.label("Support", 99) == ("Medium", True)
    assert names.label("Support", 100) == ("High", True)
    assert names.label("Support", -5) == ("Low", True)
    assert names.label("Billing", 69) == ("Routine", True)
    assert names.label("Billing", 70) == ("Urgent", True)
    # A queue RT shows as numbers is put in buckets.
    assert names.label("Ops", 60) == ("Medium", False)
    assert [label for label, _ in names.all_labels()] == ["Low", "Routine", "Medium", "Urgent", "High"]


def test_priority_names_switched_off_or_rt44() -> None:
    off = PriorityNames.from_settings(0, None, supports_names=True)
    assert off.label("Support", 120) == ("High", False)
    rt44 = PriorityNames.from_settings(None, None, supports_names=False)
    assert rt44.label("Support", 10) == ("Low", False)
    assert [label for label, _ in rt44.all_labels()] == ["Low", "Medium", "High"]
    defaults = PriorityNames.from_settings(None, None, supports_names=True)
    assert defaults.label("Support", 50) == ("Medium", True)


# -- versions -------------------------------------------------------------------

_BASE = {
    "Tickets": frozenset({"id", "ismerged", "sla"}),
    "Transactions": frozenset({"id", "timetaken"}),
    "Attachments": frozenset({"id"}),
    "CustomRoles": frozenset({"id"}),
}


def test_rt44() -> None:
    assert classify(dict(_BASE)) == "4.4"


def test_rt50() -> None:
    assert classify({**_BASE, "Configurations": frozenset({"id"})}) == "5.0"


def test_rt60() -> None:
    columns = {
        **_BASE,
        "Configurations": frozenset({"id"}),
        "Tickets": _BASE["Tickets"] | {"description"},
        "Transactions": _BASE["Transactions"] | {"timeworker", "timeworkeddate"},
    }
    assert classify(columns) == "6.0"
    assert classify({**_BASE, "Configurations": frozenset({"id"}), "SavedSearches": frozenset({"id"})}) == "6.0"


def test_older_rt_and_other_databases_are_refused() -> None:
    older = {**_BASE, "Tickets": frozenset({"id"})}
    del older["CustomRoles"]
    with pytest.raises(UnsupportedSchemaError, match="older than 4.4"):
        classify(older)
    with pytest.raises(UnsupportedSchemaError, match="no RT tables"):
        classify({"issues": frozenset({"id"})})
