"""Rights on queues to memberships, privilege, administrators."""

from __future__ import annotations

from typing import Any

from specivo_rt_migration.access import build_directory, role_for

USERS = [
    {"id": 1, "name": "RT_System"},
    {"id": 6, "name": "Nobody"},
    {"id": 10, "name": "ann"},
    {"id": 11, "name": "ben"},
]


def _groups() -> list[dict[str, Any]]:
    return [
        {"id": 100, "name": "Privileged", "domain": "SystemInternal", "instance": 0},
        {"id": 101, "name": "Everyone", "domain": "SystemInternal", "instance": 0},
        {"id": 110, "name": "UserEquiv", "domain": "ACLEquivalence", "instance": 10},
        {"id": 111, "name": "UserEquiv", "domain": "ACLEquivalence", "instance": 11},
        {"id": 200, "name": "Agents", "domain": "UserDefined", "instance": 0},
        {"id": 300, "name": "AdminCc", "domain": "RT::Queue-Role", "instance": 7},
        {"id": 301, "name": "AdminCc", "domain": "RT::System-Role", "instance": 1},
    ]


def _members() -> list[dict[str, Any]]:
    pairs = [(100, 10), (101, 10), (101, 11), (110, 10), (111, 11), (200, 11), (300, 10), (300, 200)]
    return [{"groupid": group, "memberid": member} for group, member in pairs]


def _acl(*rows: tuple[str, int, str, str, int]) -> list[dict[str, Any]]:
    keys = ("principaltype", "principalid", "rightname", "objecttype", "objectid")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def test_role_for_rights() -> None:
    assert role_for({"AdminQueue", "ShowTicket"}) == "manager"
    assert role_for({"ModifyTicket"}) == "developer"
    assert role_for({"OwnTicket", "ShowTicket"}) == "developer"
    assert role_for({"ShowTicket", "SeeQueue"}) == "reporter"
    assert role_for({"SeeQueue", "CreateTicket"}) is None


def test_privilege_and_administrators() -> None:
    directory = build_directory(
        users=USERS,
        groups=_groups(),
        cached_members=_members(),
        acl=_acl(("Group", 110, "SuperUser", "RT::System", 1)),
        queue_ids=[7],
    )
    assert directory.system_user_ids == {1, 6}
    assert directory.privileged == {10: True, 11: False}
    assert directory.admins == {10}
    # SuperUser makes an administrator, not a member of every queue.
    assert directory.memberships == {}


def test_memberships_from_user_group_and_queue_role_rights() -> None:
    directory = build_directory(
        users=USERS,
        groups=_groups(),
        cached_members=_members(),
        acl=_acl(
            ("Group", 111, "AdminQueue", "RT::Queue", 7),
            ("Group", 200, "ShowTicket", "RT::System", 1),
            # The AdminCc role, granted globally, reaches the queue's AdminCc watchers.
            ("AdminCc", 301, "ModifyTicket", "RT::System", 1),
            ("Group", 101, "CreateTicket", "RT::System", 1),
            ("Requestor", 302, "ShowTicket", "RT::System", 1),
            ("Group", 111, "ShowTicket", "RT::Queue", 99),
            ("Group", 110, "SeeCustomField", "RT::CustomField", 4),
        ),
        queue_ids=[7, 8],
    )
    assert directory.memberships == {
        (7, "user", 11): "manager",
        (7, "group", 200): "developer",
        (8, "group", 200): "reporter",
        (7, "user", 10): "developer",
    }
    assert directory.not_carried == {
        "rights granted to everyone in RT's Everyone group": 1,
        "rights granted to the Requestor role of each ticket": 1,
        "rights on other objects (custom fields, classes, catalogs, groups)": 1,
    }
    assert directory.group_members == {200: [11]}


def test_rights_of_rt_privileged_and_everyone_groups() -> None:
    directory = build_directory(
        users=USERS,
        groups=_groups(),
        cached_members=_members(),
        acl=_acl(
            ("Group", 100, "ShowTicket", "RT::System", 1),
            ("Group", 100, "ModifyTicket", "RT::Queue", 8),
            ("Group", 100, "SeeQueue", "RT::Queue", 7),
            ("Group", 100, "AdminQueue", "RT::Queue", 99),  # a queue not exported
            ("Group", 101, "CreateTicket", "RT::System", 1),
            ("Group", 101, "ShowTicket", "RT::Queue", 7),
            ("Group", 101, "ReplyToTicket", "RT::Queue", 7),
        ),
        queue_ids=[7, 8],
    )
    assert directory.privileged_group == 100
    assert directory.privileged_roles == {7: "reporter", 8: "developer"}
    # Neither group makes a membership of its own.
    assert directory.memberships == {}
    assert directory.everyone_global == {"CreateTicket"}
    assert directory.everyone_rights == {7: {"ShowTicket", "ReplyToTicket"}}
    assert directory.not_carried == {
        "rights granted to everyone in RT's Privileged group": 1,
        "rights granted to everyone in RT's Everyone group": 3,
    }
