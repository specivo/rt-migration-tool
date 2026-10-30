"""People, groups and who may do what in which queue.

**Users.** Every user RT has, except its two system accounts (``RT_System``
and ``Nobody``). A privileged user (a member of RT's internal *Privileged*
group) is active unless disabled; an unprivileged one (a requestor, someone
who once sent mail) is carried so authorship is kept, but locked unless the
export is asked to make them active. A user holding RT's ``SuperUser`` right,
directly or through a group, is marked as an administrator, which Specivo
only honours when told to; that right makes no membership.

**Groups.** RT's user-defined groups, each with every user in it, directly or
through groups inside it (Specivo's groups hold users only).

**Memberships.** RT grants rights on a queue (or on every queue, globally) to
users, groups and roles. The tool works out the rights each user and
user-defined group holds on each queue, and gives it the Specivo role those
rights amount to:

- ``AdminQueue``: **Manager**;
- ``OwnTicket`` or ``ModifyTicket``: **Developer**;
- ``ShowTicket`` and nothing above: **Reporter**.

Rights granted to a queue's ``AdminCc`` or ``Cc`` role reach the users and
groups that are that queue's AdminCc or Cc watchers. Rights granted to RT's
internal *Privileged* group reach every privileged user: the export carries
them as one group of those users, with the role its rights amount to on each
queue. Rights granted to the per-ticket roles (``Owner``, ``Requestor``, a
ticket's ``Cc``) and to RT's other internal groups (*Everyone*,
*Unprivileged*) name no one in particular and make no membership; they are
counted, and what *Everyone* holds on each queue is kept for the report: it
takes in requestors and anyone who mails in, so it never makes a project
public. The rights themselves are not carried: Specivo creates roles without
permissions, for an administrator to fill in.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SYSTEM_USERS = frozenset({"rt_system", "nobody"})

MANAGER, DEVELOPER, REPORTER = "manager", "developer", "reporter"
ROLE_NAMES: dict[str, str] = {MANAGER: "Manager", DEVELOPER: "Developer", REPORTER: "Reporter"}
_RANK = {REPORTER: 1, DEVELOPER: 2, MANAGER: 3}

MANAGER_RIGHTS = frozenset({"AdminQueue"})
DEVELOPER_RIGHTS = frozenset({"OwnTicket", "ModifyTicket"})
REPORTER_RIGHTS = frozenset({"ShowTicket"})

QUEUE_ROLES = frozenset({"AdminCc", "Cc"})


def role_for(rights: Iterable[str]) -> str | None:
    """The Specivo role a set of RT rights amounts to, or ``None``."""
    held = set(rights)
    if held & MANAGER_RIGHTS:
        return MANAGER
    if held & DEVELOPER_RIGHTS:
        return DEVELOPER
    if held & REPORTER_RIGHTS:
        return REPORTER
    return None


@dataclass
class Directory:
    """Users, groups and memberships derived from RT's principals and rights."""

    # User id -> privileged?
    privileged: dict[int, bool] = field(default_factory=dict)
    admins: set[int] = field(default_factory=set)
    system_user_ids: set[int] = field(default_factory=set)
    # User-defined group id -> carried user ids in it.
    group_members: dict[int, list[int]] = field(default_factory=dict)
    # (queue id, "user"|"group", principal id) -> role.
    memberships: dict[tuple[int, str, int], str] = field(default_factory=dict)
    # RT's Privileged group: its id, and queue id -> the role its rights amount to there.
    privileged_group: int | None = None
    privileged_roles: dict[int, str] = field(default_factory=dict)
    # What RT's Everyone group holds globally, and on each queue itself.
    everyone_global: set[str] = field(default_factory=set)
    everyone_rights: dict[int, set[str]] = field(default_factory=dict)
    # What the ACL had that no membership carries, by reason.
    not_carried: dict[str, int] = field(default_factory=dict)


def build_directory(
    *,
    users: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    cached_members: Sequence[Mapping[str, Any]],
    acl: Sequence[Mapping[str, Any]],
    queue_ids: Iterable[int],
) -> Directory:
    """Work out privilege, administrators, group members and queue memberships.

    *groups* holds RT's groups of the domains ``SystemInternal``,
    ``UserDefined``, ``ACLEquivalence``, ``RT::Queue-Role`` and
    ``RT::System-Role``; *cached_members* the rows of ``CachedGroupMembers``
    for them.
    """
    directory = Directory()
    user_ids = {int(user["id"]) for user in users}
    directory.system_user_ids = {int(user["id"]) for user in users if str(user["name"]).lower() in SYSTEM_USERS}
    people = user_ids - directory.system_user_ids

    members: dict[int, set[int]] = defaultdict(set)
    for row in cached_members:
        if int(row["memberid"]) != int(row["groupid"]):
            members[int(row["groupid"])].add(int(row["memberid"]))

    by_id = {int(group["id"]): group for group in groups}
    domain = {gid: str(group.get("domain") or "").lower() for gid, group in by_id.items()}
    name = {gid: str(group.get("name") or "") for gid, group in by_id.items()}

    def users_in(group_id: int) -> set[int]:
        return {member for member in members.get(group_id, set()) if member in people}

    privileged_group = next(
        (gid for gid in by_id if domain[gid] == "systeminternal" and name[gid].lower() == "privileged"), None
    )
    privileged = users_in(privileged_group) if privileged_group is not None else set()
    directory.privileged_group = privileged_group
    directory.privileged = {user: user in privileged for user in sorted(people)}

    user_defined = sorted(gid for gid in by_id if domain[gid] == "userdefined")
    directory.group_members = {gid: sorted(users_in(gid)) for gid in user_defined}

    # Who each ACL principal is.
    equivalence = {
        gid: int(by_id[gid]["instance"] or 0)
        for gid in by_id
        if domain[gid] == "aclequivalence" and name[gid].lower() == "userequiv"
    }

    for row in acl:
        if row["rightname"] == "SuperUser" and row["objecttype"] == "RT::System":
            principal = int(row["principalid"])
            if principal in equivalence:
                directory.admins.add(equivalence[principal])
            else:
                directory.admins.update(users_in(principal))
    directory.admins &= people

    queues = sorted(set(queue_ids))
    # rights[(queue, kind, principal)] -> rights; role_rights[(queue, role)] -> rights.
    rights: dict[tuple[int, str, int], set[str]] = defaultdict(set)
    role_rights: dict[tuple[int, str], set[str]] = defaultdict(set)
    privileged_rights: dict[int, set[str]] = defaultdict(set)
    everyone_rights: dict[int, set[str]] = defaultdict(set)
    not_carried: dict[str, int] = defaultdict(int)

    for row in acl:
        object_type = str(row["objecttype"] or "")
        if object_type == "RT::Queue":
            targets = [int(row["objectid"])] if int(row["objectid"]) in queues else []
        elif object_type == "RT::System":
            targets = queues
        else:
            not_carried["rights on other objects (custom fields, classes, catalogs, groups)"] += 1
            continue
        right = str(row["rightname"])
        if right == "SuperUser":
            continue
        principal = int(row["principalid"])
        principal_type = str(row["principaltype"] or "")
        if principal_type == "Group":
            if principal in equivalence:
                user = equivalence[principal]
                if user in people:
                    for queue in targets:
                        rights[(queue, "user", user)].add(right)
            elif domain.get(principal) == "userdefined":
                for queue in targets:
                    rights[(queue, "group", principal)].add(right)
            elif principal == privileged_group and role_for({right}) is not None:
                for queue in targets:
                    privileged_rights[queue].add(right)
            elif domain.get(principal) == "systeminternal":
                not_carried[f"rights granted to everyone in RT's {name[principal]} group"] += 1
                if name[principal].lower() == "everyone":
                    if object_type == "RT::System":
                        directory.everyone_global.add(right)
                    else:
                        for queue in targets:
                            everyone_rights[queue].add(right)
            else:
                not_carried["rights granted to other groups"] += 1
        elif principal_type in QUEUE_ROLES:
            for queue in targets:
                role_rights[(queue, principal_type)].add(right)
        else:
            not_carried[f"rights granted to the {_role_label(principal_type)} role of each ticket"] += 1

    # A queue's AdminCc and Cc watchers hold what their role was granted.
    for gid, group in by_id.items():
        if domain[gid] != "rt::queue-role" or name[gid] not in QUEUE_ROLES:
            continue
        queue = int(group["instance"] or 0)
        granted = role_rights.get((queue, name[gid]))
        if queue not in queues or not granted:
            continue
        for member in members.get(gid, set()):
            if member in people:
                rights[(queue, "user", member)].update(granted)
            elif domain.get(member) == "userdefined":
                rights[(queue, "group", member)].update(granted)

    for key, held in sorted(rights.items()):
        role = role_for(held)
        if role is not None:
            directory.memberships[key] = role
    for queue, held in sorted(privileged_rights.items()):
        role = role_for(held)
        if role is not None:
            directory.privileged_roles[queue] = role
    directory.everyone_rights = dict(everyone_rights)
    directory.not_carried = dict(not_carried)
    return directory


def _role_label(principal_type: str) -> str:
    if principal_type.startswith("RT::CustomRole-"):
        return "custom"
    return principal_type or "unknown"


def higher(first: str | None, second: str | None) -> str | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _RANK[first] >= _RANK[second] else second
