"""Write an RT installation to an import bundle.

:func:`export_bundle` reads the RT database through
:class:`~specivo_rt_migration.source.RTSource`, in one read-only snapshot, and
writes the bundle with the SDK's :class:`~specivo_migration_sdk.BundleWriter`.
The phases, each reported as it ends: settings and lookups, people, queues and
memberships, custom fields, tickets (a batch at a time, queue by queue),
articles, and the count of what is not carried.

**Reproducible.** Every query orders its rows, so the same database gives the
same bundle; ``exported_at`` is the time of the run unless given.

**Resumable.** Nothing an export writes is final before its manifest: a run
that stops leaves a bundle without one, which Specivo refuses. Running the
same command again replaces such an unfinished bundle (and nothing else), and
with ``--files-path`` the attachment files already stored are kept and only
the missing ones written.
"""

from __future__ import annotations

import logging
import shutil
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, tzinfo
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from specivo_migration_sdk import (
    Activity,
    BundleWriter,
    Group,
    Membership,
    Priority,
    Project,
    Relation,
    Role,
    Status,
    Tracker,
    User,
    source_ref,
)
from specivo_migration_sdk.format import FILES_DIR, KNOWN_FILES, MANIFEST

from specivo_rt_migration import access, articles, db
from specivo_rt_migration import fields as cf
from specivo_rt_migration.config import Lifecycles, PriorityNames, priority_ref, status_ref
from specivo_rt_migration.content import DiskStorage
from specivo_rt_migration.source import RTSource, chunks
from specivo_rt_migration.tickets import (
    ACTIVITY_REF,
    TRACKER_REF,
    Context,
    TicketInfo,
    TicketWriter,
    is_ticket_uri,
    rt_time,
    ticket_number,
)

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "rt"
GENERATOR_NAME = "specivo-rt-migration"
APPROVALS_QUEUE = "___Approvals"
# The group that stands for RT's Privileged group: every privileged user carried.
PRIVILEGED_GROUP_REF = "rt-privileged"
PRIVILEGED_GROUP_NAME = "RT privileged users"

type Progress = Callable[[str], None]


def generator_version() -> str:
    try:
        return version(GENERATOR_NAME)
    except PackageNotFoundError:  # pragma: no cover - running from an uninstalled checkout
        return "0"


class ExportError(Exception):
    """The export cannot start as asked: an unknown queue, a target in the way, a bad option."""


@dataclass(kw_only=True)
class ExportOptions:
    """What to export and where. ``queues`` names queues by name or id; empty means all."""

    db_url: str
    out: Path
    instance: str
    queues: Sequence[str] = ()
    limit: int | None = None
    zip: bool | None = None
    files_path: Path | None = None
    external_storage: Path | None = None
    activate_unprivileged: bool = False
    status_category_map: dict[str, str] = field(default_factory=dict)
    cf_key_map: dict[str, str] = field(default_factory=dict)
    timezone: str | None = None
    link_hash_numbers: bool = False
    include_comment_files: bool = False
    exported_at: str | None = None
    batch_size: int = 100


@dataclass
class ExportSummary:
    """What an export wrote."""

    path: Path
    instance: str
    rt_version: str
    counts: dict[str, int]
    not_carried_over: list[str]
    guessed_statuses: list[str]
    attachment_bytes: int
    replaced_unfinished: bool = False


def _silent(message: str) -> None:
    del message


async def export_bundle(options: ExportOptions, progress: Progress = _silent) -> ExportSummary:
    """Read the RT database in *options* and write its bundle to ``options.out``."""
    if options.external_storage is not None and not options.external_storage.is_dir():
        raise ExportError(f"--external-storage-path {options.external_storage} is not a directory")
    progress(f"Reading {db.safe_url(db.normalise_source_url(options.db_url))}")
    source = RTSource(options.db_url)
    try:
        await source.connect()
        progress(f"Found {source.schema.describe()}")
        return await _Export(source, options, progress).run()
    finally:
        await source.close()


def remove_unfinished(path: Path) -> bool:
    """Remove *path* if it is a bundle directory an earlier run left without a manifest.

    Only a directory holding nothing but the files a bundle has is removed;
    anything else stays for the writer to refuse.
    """
    if not path.is_dir() or (path / MANIFEST).exists():
        return False
    entries = list(path.iterdir())
    if not entries:
        return False
    for entry in entries:
        if entry.name == FILES_DIR and entry.is_dir() and not entry.is_symlink():
            continue
        if entry.is_file() and not entry.is_symlink() and entry.name in KNOWN_FILES:
            continue
        return False
    shutil.rmtree(path)
    return True


class _Export:
    """One export run: the scope, what was written, and the writer."""

    def __init__(self, source: RTSource, options: ExportOptions, progress: Progress) -> None:
        self.source = source
        self.options = options
        self.progress = progress
        self.storage = DiskStorage(options.external_storage) if options.external_storage else None
        self.not_carried: Counter[str] = Counter()
        self.notes: list[str] = []
        self.guessed: list[str] = []
        # Project identifiers given so far: queues in id order, then article classes in id order.
        self.identifiers: set[str] = set()

    def count(self, what: str, number: int = 1) -> None:
        self.not_carried[what] += number

    # ------------------------------------------------------------------

    async def run(self) -> ExportSummary:
        source = self.source
        zone = await self._zone()
        lifecycles = Lifecycles.from_setting(await source.setting("Lifecycles"))
        priorities = PriorityNames.from_settings(
            await source.setting("EnablePriorityAsString"),
            await source.setting("PriorityAsString"),
            supports_names=source.schema.version != "4.4",
        )

        all_queues = {int(row["id"]): row for row in await source.queues()}
        queues = self._selected_queues(all_queues)
        exported = {int(row["id"]) for row in queues}

        index = {
            int(row["id"]): TicketInfo(
                id=int(row["id"]),
                effective=int(row["effectiveid"] or row["id"]),
                queue=int(row["queue"] or 0),
                type=str(row["type"] or "ticket"),
                status=str(row["status"] or ""),
            )
            for row in await source.ticket_index()
        }
        users = await source.users()
        replaced = remove_unfinished(self.options.out)
        if replaced:
            self.progress(f"Replacing the unfinished bundle an earlier run left in {self.options.out}")
        try:
            writer = BundleWriter(
                self.options.out,
                source_system=SOURCE_SYSTEM,
                source_instance=self.options.instance,
                source_version=source.schema.version,
                generator_name=GENERATOR_NAME,
                generator_version=generator_version(),
                exported_at=self.options.exported_at,
                zip=self.options.zip,
                files_path=self.options.files_path,
            )
        except FileExistsError as exc:
            raise ExportError(f"{exc}: choose a new path, or remove it first") from None

        with writer:
            self.writer = writer
            ctx = Context(
                writer=writer,
                source=source,
                storage=self.storage,
                zone=zone,
                lifecycles=lifecycles,
                priorities=priorities,
                tickets=index,
                in_bundle=set(),
                users={},
                principal_names={},
                queues=all_queues,
                exported_queues=exported,
                fields={},
                carried_fields=set(),
                custom_roles={},
                statuses=set(),
                count=self.count,
                link_hash_numbers=self.options.link_hash_numbers,
                organization=await self._organization(),
                include_comment_files=self.options.include_comment_files,
            )
            orders = self._scope(ctx, queues)
            self._lookups(ctx, queues, lifecycles, priorities)
            await self._people(ctx, users, exported)
            for queue in queues:
                self._project(queue)
            await self._memberships(ctx, users, exported)
            await self._custom_fields(ctx, exported)
            ctx.custom_roles = {int(row["id"]): str(row["name"]) for row in await source.custom_roles()}
            await self._links(ctx)
            tickets = TicketWriter(ctx)
            for queue in queues:
                await self._queue_tickets(tickets, queue, orders.get(int(queue["id"]), []))
            await self._articles(ctx, exported)
            await self._instance_notes()
            for what, number in source.unreadable.items():
                self.count(what, number)
            for what, number in sorted(self.not_carried.items()):
                if number:
                    writer.not_carried_over(what, count=number)
                    self.notes.append(f"{what} ({number})")
        self.progress(f"Wrote {self.options.out}")
        return ExportSummary(
            path=self.options.out,
            instance=self.options.instance,
            rt_version=source.schema.version,
            counts=writer.counts,
            not_carried_over=self.notes,
            guessed_statuses=self.guessed,
            attachment_bytes=ctx.attachment_bytes,
            replaced_unfinished=replaced,
        )

    async def _zone(self) -> tzinfo:
        name = self.options.timezone or await self.source.setting("Timezone")
        if not name:
            return UTC
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError):
            if self.options.timezone:
                raise ExportError(f"--timezone {name!r} is not a time zone name such as Asia/Bangkok") from None
            logger.warning("RT's time zone %r is unknown here; dates are taken in UTC", name)
            return UTC

    async def _organization(self) -> str | None:
        """RT's $Organization: from its settings, else from the URI of a link RT took as local."""
        value = await self.source.setting("Organization")
        if value:
            return str(value).strip() or None
        found = await self.source.local_uri_host()
        if found is None:
            logger.warning(
                "RT's organization is unknown: only links RT stored as local are taken as links between its tickets"
            )
        return found

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    def _selected_queues(self, queues: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        usable = [row for row in queues.values() if str(row["name"]) != APPROVALS_QUEUE]
        if not self.options.queues:
            return sorted(usable, key=lambda row: int(row["id"]))
        by_name = {str(row["name"]).lower(): row for row in usable}
        by_id = {str(row["id"]): row for row in usable}
        chosen: dict[int, dict[str, Any]] = {}
        for selector in self.options.queues:
            name = selector.strip()
            row = by_name.get(name.lower()) or by_id.get(name)
            if row is None:
                raise ExportError(f"No queue {name!r} in RT (give its name or its id)")
            chosen[int(row["id"])] = row
        return [chosen[key] for key in sorted(chosen)]

    def _scope(self, ctx: Context, queues: list[dict[str, Any]]) -> dict[int, list[int]]:
        """Decide which tickets become issues, and count those that do not."""
        exported = {int(queue["id"]) for queue in queues}
        approvals = {int(row["id"]) for row in ctx.queues.values() if str(row["name"]) == APPROVALS_QUEUE}
        orders: dict[int, list[int]] = defaultdict(list)
        merged: list[tuple[TicketInfo, int]] = []
        for info in sorted(ctx.tickets.values(), key=lambda info: info.id):
            survivor = ctx.survivor(info.id)
            if survivor is None:
                self.count("merged tickets whose surviving ticket no longer exists")
                continue
            if survivor != info.id:
                merged.append((info, survivor))
                continue
            kind = info.type.lower()
            in_scope = info.queue in exported or (kind == "approval" and info.queue in approvals)
            if not in_scope:
                continue
            if kind == "reminder":
                self.count("reminders (kept in the extra of the ticket they remind of)")
                continue
            if kind == "approval":
                self.count("approval tickets")
                continue
            if kind not in ("ticket", ""):
                self.count(f"tickets of type {info.type}")
                continue
            if info.status.lower() == "deleted":
                self.count("deleted tickets")
                continue
            orders[info.queue].append(info.id)
        if self.options.limit is not None:
            orders = defaultdict(list, {queue: ids[: self.options.limit] for queue, ids in orders.items()})
        ctx.in_bundle = {ticket for ids in orders.values() for ticket in ids}
        for info, survivor in merged:
            # A merged ticket is folded only where it would be exported itself: what its queue
            # shows must not reach a project of another queue.
            own = info.queue in exported
            if survivor in ctx.in_bundle and own and ctx.is_carried_kind(info.id):
                ctx.merged[survivor].append(info.id)
                self.count("merged tickets (folded into the ticket they were merged into)")
            elif survivor in ctx.in_bundle and not own:
                self.count("merged tickets of queues not exported (left out of the ticket they were merged into)")
            elif survivor in ctx.in_bundle:
                self.count("merged tickets that are deleted, reminders or approvals")
            elif own:
                self.count(
                    "merged tickets whose surviving ticket is not in this bundle "
                    "(another queue, deleted, a reminder or approval, or cut by --limit)"
                )
        total = len(ctx.in_bundle)
        self.progress(f"Tickets to export: {total} in {len(queues)} queues")
        return orders

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def _lookups(
        self, ctx: Context, queues: list[dict[str, Any]], lifecycles: Lifecycles, priorities: PriorityNames
    ) -> None:
        writer = self.writer
        names: dict[str, tuple[str, str | None]] = {}
        for queue in queues:
            lifecycle = lifecycles.for_queue(queue.get("lifecycle"))
            for status in lifecycle.order:
                names.setdefault(status, (status, queue.get("lifecycle")))
        for ticket in ctx.in_bundle:
            info = ctx.tickets[ticket]
            names.setdefault(info.status.lower(), (info.status, ctx.queues.get(info.queue, {}).get("lifecycle")))
        names.pop("deleted", None)
        names.setdefault("open", ("open", None))

        default_status = status_ref(lifecycles.for_queue(None).on_create or "new")
        position = 0
        for key, (status, lifecycle_name) in names.items():
            category, guessed = lifecycles.category(lifecycle_name, status)
            override = self.options.status_category_map.get(status) or self.options.status_category_map.get(key)
            if override:
                category = override
            elif guessed:
                self.guessed.append(status)
            position += 1
            writer.add(
                Status(
                    source_ref=status_ref(status),
                    name=status,
                    category=category,  # type: ignore[arg-type]
                    position=position,
                    extra={"rt_lifecycle": lifecycle_name or "default"},
                )
            )
            ctx.statuses.add(key)
        if default_status.lower() not in ctx.statuses:
            default_status = status_ref(next(iter(names)))

        writer.add(Tracker(source_ref=TRACKER_REF, name="Ticket", default_status_ref=default_status))
        labels = priorities.all_labels()
        for position, (label, lowest) in enumerate(labels, start=1):
            writer.add(
                Priority(
                    source_ref=priority_ref(label),
                    name=label,
                    position=position,
                    is_default=position == 1,
                    extra={"rt_lowest_number": lowest},
                )
            )
        writer.add(Activity(source_ref=ACTIVITY_REF, name="Work", is_default=True))
        for key, name in access.ROLE_NAMES.items():
            writer.add(Role(source_ref=key, name=name, extra={"rt_rights": _rights_for(key)}))
        origin = "RT's settings" if lifecycles.origin == "database" else "RT's defaults"
        self.progress(f"Statuses: {len(names)} (lifecycles from {origin}), priorities: {len(labels)}")

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------

    async def _people(self, ctx: Context, users: list[dict[str, Any]], exported: set[int]) -> None:
        source = self.source
        groups = await source.groups(
            "SystemInternal", "UserDefined", "ACLEquivalence", "RT::Queue-Role", "RT::System-Role"
        )
        members = await source.cached_members([int(group["id"]) for group in groups])
        self.directory = access.build_directory(
            users=users, groups=groups, cached_members=members, acl=await source.acl(), queue_ids=exported
        )
        directory = self.directory
        # A partial export carries only the people its content names, and the groups its projects have.
        wanted = await self._referenced(ctx, exported) if self.options.queues else None
        for user in users:
            user_id = int(user["id"])
            ctx.principal_names[user_id] = str(user["name"])
            if user_id in directory.system_user_ids or (wanted is not None and user_id not in wanted[0]):
                continue
            privileged = directory.privileged.get(user_id, False)
            if user["disabled"]:
                status = "locked"
            elif privileged or self.options.activate_unprivileged:
                status = "active"
            else:
                status = "locked"
            extra: dict[str, Any] = {"rt_user": user_id, "privileged": privileged}
            for key in ("organization", "nickname", "timezone"):
                if user.get(key):
                    extra[key] = user[key]
            if user["disabled"]:
                extra["disabled"] = True
            self.writer.add(
                User(
                    source_ref=str(user_id),
                    login=str(user["name"]),
                    display_name=str(user.get("realname") or user["name"]).strip() or str(user["name"]),
                    email=(str(user["emailaddress"]).strip() or None) if user.get("emailaddress") else None,
                    status=status,  # type: ignore[arg-type]
                    is_admin=user_id in directory.admins,
                    language=str(user["lang"]) if user.get("lang") else None,
                    created_at=rt_time(user.get("created")),
                    updated_at=rt_time(user.get("lastupdated")),
                    extra=extra,
                )
            )
            ctx.users[user_id] = str(user["name"])
        self.count("passwords (RT's hashes cannot be used by Specivo)", len(ctx.users))
        user_defined = [group for group in groups if str(group["domain"]).lower() == "userdefined"]
        for group in user_defined:
            ctx.principal_names[int(group["id"])] = str(group["name"])
        if wanted is not None:
            user_defined = [group for group in user_defined if int(group["id"]) in wanted[1]]
        for group in user_defined:
            group_id = int(group["id"])
            extra = {"rt_group": group_id}
            if group.get("description"):
                extra["description"] = group["description"]
            self.writer.add(
                Group(
                    source_ref=str(group_id),
                    name=str(group["name"]),
                    member_refs=[str(user) for user in directory.group_members.get(group_id, []) if user in ctx.users],
                    extra=extra,
                )
            )
        self.groups = {int(group["id"]) for group in user_defined}
        # RT's Privileged group, only when its rights give it a role on a queue exported.
        self.privileged_roles = {queue: role for queue, role in directory.privileged_roles.items() if queue in exported}
        if self.privileged_roles:
            self.writer.add(
                Group(
                    source_ref=PRIVILEGED_GROUP_REF,
                    name=PRIVILEGED_GROUP_NAME,
                    member_refs=[str(user) for user in sorted(ctx.users) if directory.privileged.get(user, False)],
                    extra={"rt_group": directory.privileged_group, "rt_system_group": "Privileged"},
                )
            )
        written = len(user_defined) + (1 if self.privileged_roles else 0)
        unprivileged = sum(1 for user in ctx.users if not directory.privileged.get(user, False))
        self.progress(f"Users: {len(ctx.users)} ({unprivileged} unprivileged), groups: {written}")

    async def _referenced(self, ctx: Context, exported: set[int]) -> tuple[set[int], set[int]]:
        """The users and user-defined groups the selected queues' content names or gives a membership."""
        tickets = ctx.in_bundle | {merged for survivor in ctx.in_bundle for merged in ctx.merged.get(survivor, [])}
        # Reminders of the tickets name their owners in the issues' extra.
        reminders = {
            int(link["localbase"])
            for link in await self.source.links(sorted(tickets))
            if int(link.get("localbase") or 0) in ctx.tickets
            and ctx.tickets[int(link["localbase"])].type.lower() == "reminder"
        }
        users = await self.source.ticket_people(sorted(tickets | reminders))
        scope = await self._article_scope(exported)
        if scope is not None:
            wanted = scope[3]
            users.update(int(row.get(key) or 0) for row in wanted for key in ("creator", "lastupdatedby"))
            articles_ids = [int(row["id"]) for row in wanted]
            users.update(
                int(row["creator"] or 0) for row in await self.source.transactions("RT::Article", articles_ids)
            )
        groups: set[int] = set()
        for queue, kind, principal in self.directory.memberships:
            if queue not in exported:
                continue
            if kind == "user":
                users.add(principal)
            else:
                groups.add(principal)
                users.update(self.directory.group_members.get(principal, []))
        return users, groups

    async def _memberships(self, ctx: Context, users: list[dict[str, Any]], exported: set[int]) -> None:
        written = 0
        for (queue, kind, principal), role in sorted(self.directory.memberships.items()):
            known = ctx.users if kind == "user" else self.groups
            if principal not in known or queue not in exported:
                continue
            self.writer.add(
                Membership(
                    source_ref=source_ref(queue, kind, principal),
                    project_ref=str(queue),
                    principal_kind=kind,  # type: ignore[arg-type]
                    principal_ref=str(principal),
                    role_refs=[role],
                )
            )
            written += 1
        for queue, role in sorted(self.privileged_roles.items()):
            self.writer.add(
                Membership(
                    source_ref=source_ref(queue, "group", PRIVILEGED_GROUP_REF),
                    project_ref=str(queue),
                    principal_kind="group",
                    principal_ref=PRIVILEGED_GROUP_REF,
                    role_refs=[role],
                )
            )
            written += 1
        for what, number in self.directory.not_carried.items():
            self.count(what, number)
        self._everyone(ctx, exported)
        acl_rows = await self.source.count("ACL")
        self.count("rights (RT's ACL: Specivo roles are created without permissions)", acl_rows)
        self.progress(f"Memberships: {written}")

    def _everyone(self, ctx: Context, exported: set[int]) -> None:
        """Name, for the report, the rights RT's Everyone group holds: never carried, for an admin to weigh."""
        directory = self.directory
        if directory.everyone_global:
            what = (
                f"rights granted to RT's Everyone group on every queue: {', '.join(sorted(directory.everyone_global))}"
            )
            self.writer.not_carried_over(what)
            self.notes.append(what)
        for queue, held in sorted(directory.everyone_rights.items()):
            if queue not in exported or not held:
                continue
            name = str(ctx.queues[queue]["name"])
            what = f"rights granted to RT's Everyone group on queue {name}: {', '.join(sorted(held))}"
            self.writer.not_carried_over(what, entity_type="project", source_ref=str(queue))
            self.notes.append(what)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def _project(self, queue: dict[str, Any]) -> None:
        queue_id = int(queue["id"])
        extra: dict[str, Any] = {"rt_queue": queue_id, "lifecycle": queue.get("lifecycle") or "default"}
        for key in ("correspondaddress", "commentaddress", "subjecttag"):
            if queue.get(key):
                extra[key] = queue[key]
        everyone = self.directory.everyone_global | self.directory.everyone_rights.get(queue_id, set())
        if everyone:
            # Not carried: the project stays private. Kept so an admin can decide to make it public.
            extra["rt_everyone_rights"] = sorted(everyone)
        self.writer.add(
            Project(
                source_ref=str(queue_id),
                identifier=self._identifier(str(queue["name"]), f"queue-{queue_id}"),
                name=str(queue["name"]),
                description=str(queue.get("description") or "") or None,
                status=9 if int(queue.get("disabled") or 0) else 1,
                modules=["issue_tracking", "time_tracking", "wiki"],
                created_at=rt_time(queue.get("created")),
                updated_at=rt_time(queue.get("lastupdated")),
                extra=extra,
            )
        )

    def _identifier(self, name: str, fallback: str) -> str:
        """A project identifier from *name*, made unique in the bundle by ``-2``, ``-3``, ..."""
        base = _identifier(name, fallback)
        candidate, number = base, 1
        while candidate in self.identifiers:
            number += 1
            suffix = f"-{number}"
            candidate = base[: 100 - len(suffix)].rstrip("-") + suffix
        self.identifiers.add(candidate)
        return candidate

    # ------------------------------------------------------------------
    # Custom fields
    # ------------------------------------------------------------------

    async def _custom_fields(self, ctx: Context, exported: set[int]) -> None:
        source = self.source
        rows = {int(row["id"]): row for row in await source.custom_fields()}
        names: dict[int, list[str]] = defaultdict(list)
        for row in await source.custom_field_values():
            names[int(row["customfield"])].append(str(row["name"]))
        ticket_fields = cf.build_fields(list(rows.values()), choices={}, key_overrides=self.options.cf_key_map)
        used = await self._used_values([fid for fid, info in ticket_fields.items() if info.rt_type == "Select"])
        for field_id, info in ticket_fields.items():
            if info.rt_type == "Select":
                info.choices = list(dict.fromkeys([*names.get(field_id, []), *used.get(field_id, [])]))
        applied: dict[int, set[int]] = defaultdict(set)
        for row in await source.object_custom_fields():
            applied[int(row["customfield"])].add(int(row["objectid"] or 0))
        written = 0
        for field_id, info in ticket_fields.items():
            ctx.fields[field_id] = info
            if info.is_file:
                continue
            queues = applied.get(field_id, set())
            is_for_all = 0 in queues
            projects = sorted(queue for queue in queues if queue in exported)
            if not is_for_all and not projects:
                continue
            self.writer.add(
                cf.definition(
                    info,
                    rows[field_id],
                    is_for_all=is_for_all,
                    project_refs=[str(queue) for queue in projects],
                    suggestions=names.get(field_id, []),
                )
            )
            ctx.carried_fields.add(field_id)
            written += 1
        by_lookup: Counter[str] = Counter(str(row.get("lookuptype") or "") for row in rows.values())
        labels = {
            "RT::Queue": "custom fields of queues",
            "RT::User": "custom fields of users",
            "RT::Group": "custom fields of groups",
            "RT::Queue-RT::Ticket-RT::Transaction": "custom fields of transactions",
            "RT::Catalog-RT::Asset": "custom fields of assets",
        }
        for lookup, number in by_lookup.items():
            if lookup in labels:
                self.count(labels[lookup], number)
                values = await source.count_custom_values_of([lookup])
                if values:
                    self.count(f"values of {labels[lookup]}", values)
        self.progress(f"Custom fields: {written}")

    async def _used_values(self, field_ids: list[int]) -> dict[int, list[str]]:
        """Values Select fields hold on tickets, in case RT no longer offers them."""
        if not field_ids:
            return {}
        from sqlalchemy import select

        t = self.source.tables.ObjectCustomFieldValues
        found: dict[int, list[str]] = defaultdict(list)
        for piece in chunks(field_ids):
            stmt = (
                select(t.c.customfield, t.c.content)
                .where(t.c.customfield.in_(piece), t.c.objecttype == "RT::Ticket", t.c.disabled == 0)
                .distinct()
                .order_by(t.c.customfield, t.c.content)
            )
            for row in (await self.source.conn.execute(stmt)).mappings():
                if row["content"] is not None:
                    found[int(row["customfield"])].append(str(row["content"]))
        return found

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    async def _links(self, ctx: Context) -> None:
        """Read every link once: parents, relations, reminders and links to other things."""
        relations: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
        parents_seen: set[int] = set()
        async for link in self.source.all_links():
            kind = str(link.get("type") or "")
            base = int(link.get("localbase") or 0) or ticket_number(link.get("base"), ctx.organization)
            target = int(link.get("localtarget") or 0) or ticket_number(link.get("target"), ctx.organization)
            if kind == "MergedInto":
                continue
            if base and target and ctx.tickets.get(base) and ctx.tickets[base].type.lower() == "reminder":
                survivor = ctx.survivor(target)
                if survivor is not None:
                    ctx.reminders[survivor].append(base)
                continue
            if base and not target:
                self._uri_link(ctx, base, kind, str(link.get("target") or ""))
                continue
            if target and not base:
                self._uri_link(ctx, target, _REVERSE.get(kind, kind), str(link.get("base") or ""))
                continue
            if not base or not target:
                continue
            from_target = ctx.issue_target(base)
            to_target = ctx.issue_target(target)
            if from_target is None or to_target is None:
                self.count("links to tickets that are deleted, reminders, approvals or no longer exist")
                continue
            if not from_target[1] and not to_target[1]:
                continue
            from_ref, to_ref = from_target[0], to_target[0]
            if from_ref == to_ref:
                continue
            extra = {"rt_link": int(link["id"]), "rt_type": kind}
            if kind == "MemberOf" and int(from_ref) in ctx.in_bundle:
                child = int(from_ref)
                if child not in parents_seen:
                    parents_seen.add(child)
                    ctx.parents[child] = int(to_ref)
                    continue
                self.count("second parents of a ticket (kept as related)")
                relations.setdefault((from_ref, to_ref, "relates"), (str(link["id"]), extra))
            elif kind == "MemberOf":
                continue  # The child is in another bundle, which sets its parent.
            elif kind == "DependsOn":
                relations.setdefault((to_ref, from_ref, "blocks"), (str(link["id"]), extra))
            elif kind == "RefersTo":
                pair = tuple(sorted((from_ref, to_ref), key=int))
                relations.setdefault((pair[0], pair[1], "relates"), (str(link["id"]), extra))
            else:
                self.count(f"links of type {kind}")
        for key in [key for key in relations if _in_one_line(ctx.parents, int(key[0]), int(key[1]))]:
            # Specivo refuses a relation between an issue and its ancestor; the parent says it already.
            del relations[key]
            self.count("relations between a ticket and its parent or child (the parent is kept)")
        for (from_ref, to_ref, relation_type), (ref, extra) in sorted(
            relations.items(), key=lambda item: int(item[1][0])
        ):
            self.writer.add(
                Relation(
                    source_ref=ref,
                    from_ref=from_ref,
                    to_ref=to_ref,
                    relation_type=relation_type,  # type: ignore[arg-type]
                    extra=extra,
                )
            )
        self.progress(f"Relations: {len(relations)}, parents: {len(ctx.parents)}")

    def _uri_link(self, ctx: Context, ticket: int, kind: str, uri: str) -> None:
        survivor = ctx.survivor(ticket)
        if survivor is None or survivor not in ctx.in_bundle:
            return
        ctx.uri_links[survivor].append({"type": kind, "uri": uri})
        if is_ticket_uri(uri):
            self.count("links to tickets of other RT servers (kept in the issue's extra)")
        else:
            self.count("links to other things than tickets (kept in the issue's extra)")

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    async def _queue_tickets(self, tickets: TicketWriter, queue: dict[str, Any], order: list[int]) -> None:
        done = 0
        for batch in chunks(order, self.options.batch_size):
            await tickets.write(batch)
            done += len(batch)
            if done < len(order):
                self.progress(f"Queue {queue['name']}: {done}/{len(order)} tickets")
        self.progress(f"Queue {queue['name']}: {len(order)} tickets")

    # ------------------------------------------------------------------
    # Articles
    # ------------------------------------------------------------------

    async def _article_scope(
        self, exported: set[int]
    ) -> (
        tuple[dict[int, dict[str, Any]], dict[int, articles.Placement], list[dict[str, Any]], list[dict[str, Any]]]
        | None
    ):
        """The classes, where each goes, every article, and the live articles of carried classes; ``None`` if none."""
        source = self.source
        if not source.schema.has_table("Articles"):
            return None
        all_articles = await source.articles()
        if not all_articles:
            return None
        classes = {int(row["id"]): row for row in await source.classes()}
        placements = articles.place_classes(list(classes.values()), await source.object_classes(), exported)
        if self.options.queues:
            # A partial export carries only the classes of its own queues.
            placements = {key: value for key, value in placements.items() if not value.own_project}
        wanted = [
            row
            for row in all_articles
            if not int(row.get("disabled") or 0) and int(row.get("class") or 0) in placements
        ]
        return classes, placements, all_articles, wanted

    async def _articles(self, ctx: Context, exported: set[int]) -> None:
        source = self.source
        scope = await self._article_scope(exported)
        if scope is None:
            return
        classes, placements, all_articles, wanted = scope
        fields = {
            int(row["id"]): articles.ArticleField(
                id=int(row["id"]),
                name=str(row["name"]),
                rt_type=str(row["type"] or ""),
                order=int(row.get("sortorder") or 0),
            )
            for row in await source.custom_fields()
            if row.get("lookuptype") == articles.ARTICLE_LOOKUP
        }
        live = [row for row in all_articles if not int(row.get("disabled") or 0)]
        if len(live) < len(all_articles):
            self.count("disabled articles", len(all_articles) - len(live))
        skipped = len(live) - len(wanted)
        if skipped:
            self.count("articles of classes outside the exported queues", skipped)
        ids = [int(row["id"]) for row in wanted]
        transactions: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in await source.transactions("RT::Article", ids):
            transactions[int(row["objectid"])].append(row)
        values: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in await source.custom_values("RT::Article", ids):
            values[int(row["objectid"])].append(row)

        own_projects: set[int] = set()
        identifiers = {
            class_id: self._identifier(
                f"kb-{classes.get(class_id, {}).get('name') or f'Class {class_id}'}", f"kb-{class_id}"
            )
            for class_id in sorted({int(row["class"]) for row in wanted if placements[int(row["class"])].own_project})
        }
        titles: dict[str, set[str]] = defaultdict(set)
        pages = 0
        for article in wanted:
            class_id = int(article["class"])
            placement = placements[class_id]
            class_row = classes.get(class_id, {})
            if placement.own_project and class_id not in own_projects:
                own_projects.add(class_id)
                name = str(class_row.get("name") or f"Class {class_id}")
                self.writer.add(
                    Project(
                        source_ref=placement.project_ref,
                        identifier=identifiers[class_id],
                        name=f"Articles: {name}",
                        description=str(class_row.get("description") or "") or None,
                        status=9 if int(class_row.get("disabled") or 0) else 1,
                        modules=["wiki"],
                        created_at=rt_time(class_row.get("created")),
                        extra={"rt_class": class_id},
                    )
                )
            versions = articles.revisions(
                article,
                transactions.get(int(article["id"]), []),
                values.get(int(article["id"]), []),
                fields,
                self.storage,
                ctx.user_ref,
            )
            title = articles.unique_title(
                str(article.get("name") or ""), int(article["id"]), titles[placement.project_ref]
            )
            self.writer.add(
                articles.page(
                    article,
                    project_ref=placement.project_ref,
                    title=title,
                    versions=versions,
                    class_name=str(class_row.get("name") or class_id),
                )
            )
            pages += 1
        self.progress(f"Articles: {pages} wiki pages")

    # ------------------------------------------------------------------
    # What is not carried
    # ------------------------------------------------------------------

    async def _instance_notes(self) -> None:
        source = self.source
        t = source.tables
        self.count("assets", await source.count("Assets"))
        self.count("asset catalogs", await source.count("Catalogs"))
        self.count("scrips", await source.count("Scrips"))
        self.count("templates", await source.count("Templates"))
        saved = await source.count_attributes("SavedSearch")
        if source.schema.has_table("SavedSearches"):
            saved += await source.count("SavedSearches", t.SavedSearches.c.disabled == 0)
        self.count("saved searches", saved)
        dashboards = await source.count_attributes("Dashboard")
        if source.schema.has_table("Dashboards"):
            dashboards += await source.count("Dashboards", t.Dashboards.c.disabled == 0)
        self.count("dashboards", dashboards)
        if source.schema.has_table("Topics"):
            self.count("article topics", await source.count("Topics"))
        if source.schema.has_column("Queues", "sladisabled"):
            self.count("queues with service levels (SLA)", await source.count("Queues", t.Queues.c.sladisabled == 0))


def _in_one_line(parents: dict[int, int], first: int, second: int) -> bool:
    """Whether one of two tickets is the other's parent, grandparent, and so on."""
    for child, ancestor in ((first, second), (second, first)):
        seen: set[int] = set()
        current = parents.get(child)
        while current is not None and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = parents.get(current)
    return False


_REVERSE = {
    "MemberOf": "HasMember",
    "DependsOn": "DependedOnBy",
    "RefersTo": "ReferredToBy",
}


def _rights_for(role: str) -> list[str]:
    return sorted(
        {
            access.MANAGER: access.MANAGER_RIGHTS,
            access.DEVELOPER: access.DEVELOPER_RIGHTS,
            access.REPORTER: access.REPORTER_RIGHTS,
        }[role]
    )


def _identifier(name: str, fallback: str) -> str:
    """A project identifier from a name: lower case, ``a-z 0-9 -``; *fallback* for a name in another script."""
    text = "".join(char if char.isascii() and char.isalnum() else "-" for char in name.lower())
    text = "-".join(part for part in text.split("-") if part)
    return text[:100] or fallback
