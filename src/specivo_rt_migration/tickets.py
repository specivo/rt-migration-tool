"""Tickets to issues: their description, history, files, watchers and time.

A batch of surviving tickets is read with everything that hangs off them and
the tickets merged into them, and written as issues, journals, attachments,
watchers and time entries. ``docs/carried-over.md`` describes the mapping;
the comments here say why.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from specivo_migration_sdk import (
    Attachment,
    BundleWriter,
    CustomValue,
    Issue,
    Journal,
    JournalDetail,
    TimeEntry,
    Watcher,
)

from specivo_rt_migration import fields as cf
from specivo_rt_migration.config import Lifecycles, PriorityNames, priority_ref, status_ref
from specivo_rt_migration.content import ContentUnavailableError, DiskStorage
from specivo_rt_migration.markdown import html_to_markdown, plain_to_markdown
from specivo_rt_migration.mime import MailMessage, Part, build_tree, take_apart
from specivo_rt_migration.references import TicketIndex, link_references
from specivo_rt_migration.source import RTSource

logger = logging.getLogger(__name__)

TRACKER_REF = "ticket"
ACTIVITY_REF = "work"
# RT writes this for a date that was never set.
_EPOCH = datetime(1970, 1, 1)
# RT's URI of a ticket: fsck.com-rt://<Organization>/ticket/<id>.
_TICKET_URI_RE = re.compile(r"^fsck\.com-rt://([^/]*)/ticket/(\d+)$")

# Transactions carried as journals, and those only counted.
MESSAGE_TYPES = frozenset({"Correspond", "Comment"})
NOT_CARRIED_TYPES: dict[str, str] = {
    "EmailRecord": "copies of mail RT sent (EmailRecord transactions)",
    "CommentEmailRecord": "copies of comment mail RT sent (CommentEmailRecord transactions)",
    "Forward Transaction": "forwarded messages (Forward transactions)",
    "Forward Ticket": "forwarded tickets (Forward transactions)",
    "AddReminder": "reminder changes (kept in the issue's extra)",
    "OpenReminder": "reminder changes (kept in the issue's extra)",
    "ResolveReminder": "reminder changes (kept in the issue's extra)",
    "SystemError": "RT system errors recorded on tickets",
    "Told": "times RT told requestors about a ticket",
}

# RT link types, as relation names in history, seen from the ticket they are recorded on.
_LINK_HISTORY_NAMES = {
    "DependsOn": "blocked_by",
    "DependedOnBy": "blocks",
    "RefersTo": "relates",
    "ReferredToBy": "relates",
    "HasMember": "has_child",
    "MergedInto": "merged",
}


def rt_time(value: Any) -> datetime | None:
    """A timestamp RT stored (naive UTC), or ``None`` for unset (NULL or 1970-01-01 00:00:00)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    naive = value.replace(tzinfo=None) if value.tzinfo is not None else value
    if naive <= _EPOCH:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def local_date(value: datetime | None, zone: tzinfo) -> date | None:
    return None if value is None else value.astimezone(zone).date()


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def hours(minutes: int) -> Decimal:
    """Minutes as hours, to the hundredth (Specivo keeps two decimals)."""
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ticket_number(uri: str | None, organization: str | None) -> int | None:
    """The number of a ticket of this RT named by *uri*, or ``None``.

    A ticket URI names a ticket of this RT only when its host is this RT's
    *organization* (compared as RT does, ignoring case); another RT's tickets
    have numbers of their own. Without an organization nothing is taken as local.
    """
    match = _TICKET_URI_RE.match((uri or "").strip())
    if match is None or organization is None or match.group(1).casefold() != organization.casefold():
        return None
    return int(match.group(2))


def is_ticket_uri(uri: str | None) -> bool:
    """Whether *uri* names a ticket of some RT, this one or another."""
    return _TICKET_URI_RE.match((uri or "").strip()) is not None


@dataclass(frozen=True, slots=True)
class TicketInfo:
    """What the export knows about every ticket of RT, from :meth:`RTSource.ticket_index`.

    One is held for each ticket RT has for the whole run, hence slots.
    """

    id: int
    effective: int
    queue: int
    type: str
    status: str


@dataclass
class Context:
    """Everything a batch needs to know about the rest of the export."""

    writer: BundleWriter
    source: RTSource
    storage: DiskStorage | None
    zone: tzinfo
    lifecycles: Lifecycles
    priorities: PriorityNames
    tickets: dict[int, TicketInfo]
    # Surviving tickets in this bundle.
    in_bundle: set[int]
    users: dict[int, str]  # user id -> login, for carried users
    principal_names: dict[int, str]  # any principal id -> a name for history
    queues: dict[int, dict[str, Any]]
    exported_queues: set[int]
    fields: dict[int, cf.FieldInfo]
    carried_fields: set[int]
    custom_roles: dict[int, str]
    statuses: set[str]
    count: Callable[[str, int], None]
    link_hash_numbers: bool = False
    # RT's $Organization, the host of its ticket URIs; None when the export cannot tell.
    organization: str | None = None
    # Files of comments are left out unless asked for: Specivo attachments have no private flag.
    include_comment_files: bool = False
    # From the pass over all links.
    parents: dict[int, int] = field(default_factory=dict)
    uri_links: dict[int, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))
    reminders: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    # Merged tickets folded into a ticket of this bundle, by the ticket they survive in.
    merged: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    attachment_bytes: int = 0

    def survivor(self, ticket_id: int) -> int | None:
        """The ticket *ticket_id* lives on in: itself, or the one it was merged into."""
        seen: set[int] = set()
        current = ticket_id
        while current not in seen:
            seen.add(current)
            info = self.tickets.get(current)
            if info is None:
                return None
            if info.effective == current or info.effective == 0:
                return current
            current = info.effective
        return None

    def is_carried_kind(self, ticket_id: int) -> bool:
        """A ticket of the kind the export carries: a ticket (not a reminder or approval), not deleted."""
        info = self.tickets.get(ticket_id)
        return info is not None and info.type.lower() in ("ticket", "") and info.status.lower() != "deleted"

    def issue_target(self, ticket_id: int) -> tuple[str, bool] | None:
        """The issue ref *ticket_id* resolves to, and whether that issue is in this bundle."""
        survivor = self.survivor(ticket_id)
        if survivor is None or not self.is_carried_kind(survivor):
            return None
        return str(survivor), survivor in self.in_bundle

    def user_ref(self, user_id: Any) -> str | None:
        if user_id is None:
            return None
        return str(user_id) if int(user_id) in self.users else None

    def ticket_index(self) -> TicketIndex:
        return TicketIndex(resolve=self.issue_target)

    def project_ref(self, queue_id: int) -> str:
        return str(queue_id)

    def queue_name(self, queue_id: int) -> str:
        queue = self.queues.get(queue_id)
        return str(queue["name"]) if queue else f"Queue {queue_id}"


@dataclass
class _Batch:
    """The rows of one batch of tickets."""

    tickets: dict[int, dict[str, Any]]
    transactions: dict[int, list[dict[str, Any]]]
    parts: dict[int, list[Part]]
    values: dict[int, list[dict[str, Any]]]
    values_by_id: dict[int, dict[str, Any]]
    long_values: dict[int, dict[str, Any]]
    roles: dict[int, dict[str, list[int]]]
    reminders: dict[int, dict[str, Any]]


class TicketWriter:
    """Writes the issues of one batch of surviving tickets at a time."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.index = ctx.ticket_index()

    # ------------------------------------------------------------------
    # Reading a batch
    # ------------------------------------------------------------------

    async def _read(self, survivors: Sequence[int]) -> _Batch:
        source = self.ctx.source
        ids = sorted({*survivors, *(m for s in survivors for m in self.ctx.merged.get(s, []))})
        tickets = {int(row["id"]): row for row in await source.tickets(ids)}
        transactions: dict[int, list[dict[str, Any]]] = defaultdict(list)
        all_transactions = await source.transactions("RT::Ticket", ids)
        for row in all_transactions:
            transactions[int(row["objectid"])].append(row)
        message_ids = [int(row["id"]) for row in all_transactions if self._has_message(row)]
        rows_by_txn: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in await source.attachments(message_ids):
            rows_by_txn[int(row["transactionid"])].append(row)
        parts = {txn: build_tree(rows) for txn, rows in rows_by_txn.items()}
        parts_flat: dict[int, list[Part]] = {txn: tops for txn, tops in parts.items()}

        values: dict[int, list[dict[str, Any]]] = defaultdict(list)
        values_by_id: dict[int, dict[str, Any]] = {}
        for row in await source.custom_values("RT::Ticket", ids):
            values[int(row["objectid"])].append(row)
            values_by_id[int(row["id"])] = row
        missing = {
            int(ref)
            for row in all_transactions
            if row["type"] == "CustomField"
            for ref in (row.get("oldreference"), row.get("newreference"))
            if ref and int(ref) not in values_by_id
        }
        for row in await source.custom_values_by_id(sorted(missing)):
            values_by_id[int(row["id"])] = row

        long_values = await source.object_contents(
            [int(row["id"]) for row in all_transactions if row.get("referencetype") == "RT::ObjectContent"]
        )

        roles: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        role_groups = await source.ticket_roles(list(survivors))
        members = await source.cached_members([int(group["id"]) for group in role_groups])
        by_group: dict[int, list[int]] = defaultdict(list)
        for row in members:
            if int(row["memberid"]) != int(row["groupid"]):
                by_group[int(row["groupid"])].append(int(row["memberid"]))
        for group in role_groups:
            roles[int(group["instance"])][str(group["name"])] = sorted(set(by_group.get(int(group["id"]), [])))

        reminder_ids = sorted({r for s in survivors for r in self.ctx.reminders.get(s, [])})
        reminders = {int(row["id"]): row for row in await source.tickets(reminder_ids)}
        return _Batch(tickets, transactions, parts_flat, values, values_by_id, long_values, roles, reminders)

    @staticmethod
    def _has_message(row: Mapping[str, Any]) -> bool:
        return row["type"] in ("Create", "Correspond", "Comment")

    # ------------------------------------------------------------------
    # Writing a batch
    # ------------------------------------------------------------------

    async def write(self, survivors: Sequence[int]) -> None:
        batch = await self._read(survivors)
        for ticket_id in survivors:
            ticket = batch.tickets.get(ticket_id)
            if ticket is None:
                continue
            _OneTicket(self, batch, ticket).write()


class _OneTicket:
    """One surviving ticket and the tickets merged into it."""

    def __init__(self, owner: TicketWriter, batch: _Batch, ticket: dict[str, Any]) -> None:
        self.owner = owner
        self.ctx = owner.ctx
        self.batch = batch
        self.ticket = ticket
        self.id = int(ticket["id"])
        self.ref = str(self.id)
        self.queue = int(ticket["queue"])
        # Every file written for this issue, by RT attachment id, for links in HTML.
        self.files: dict[int, str] = {}

    @property
    def writer(self) -> BundleWriter:
        return self.ctx.writer

    # ------------------------------------------------------------------

    def write(self) -> None:
        merged = sorted(self.ctx.merged.get(self.id, []))
        transactions = sorted(
            (row for tid in [self.id, *merged] for row in self.batch.transactions.get(tid, [])),
            key=lambda row: int(row["id"]),
        )
        create = next(
            (row for row in transactions if row["type"] == "Create" and int(row["objectid"]) == self.id), None
        )
        description, create_journal = self._description(create)
        watchers, roles = self._roles()
        self.writer.add(self._issue(description, merged, roles))
        if create_journal is not None:
            self.writer.add(create_journal)
        if create is not None:
            # RT records the time worked given on creation as the Create transaction's TimeTaken.
            self._time_entry(create)
        self._custom_file_values()
        for user in watchers:
            self.writer.add(Watcher(container_kind="issue", container_ref=self.ref, user_ref=str(user)))
        self._history([row for row in transactions if row is not create])

    # -- the issue -------------------------------------------------------

    def _issue(self, description: str | None, merged: list[int], roles: dict[str, list[str]]) -> Issue:
        t = self.ticket
        ctx = self.ctx
        queue_name = ctx.queue_name(self.queue)
        status = str(t.get("status") or "new")
        lifecycle = (ctx.queues.get(self.queue) or {}).get("lifecycle")
        label, named = ctx.priorities.label(queue_name, t.get("priority"))
        created = rt_time(t.get("created"))
        resolved = rt_time(t.get("resolved"))
        extra: dict[str, Any] = {
            "rt_ticket": self.id,
            "rt_queue": queue_name,
            "rt_status": status,
            "priority": t.get("priority"),
            "initial_priority": t.get("initialpriority"),
            "final_priority": t.get("finalpriority"),
            "type": t.get("type"),
            "time_worked_minutes": t.get("timeworked"),
            "time_estimated_minutes": t.get("timeestimated"),
            "time_left_minutes": t.get("timeleft"),
        }
        if not named:
            extra["priority_bucket"] = label
        for key, column in (("told", "told"), ("started", "started"), ("resolved", "resolved")):
            moment = rt_time(t.get(column))
            if moment is not None:
                extra[key] = iso(moment)
        if t.get("sla"):
            extra["sla"] = t["sla"]
        if merged:
            extra["merged_tickets"] = merged
        if roles:
            extra["roles"] = roles
        if roles.get("Requestor"):
            extra["requestors"] = roles["Requestor"]
        links = self.ctx.uri_links.get(self.id)
        if links:
            extra["links"] = links
        reminders = [self._reminder(rid) for rid in self.ctx.reminders.get(self.id, [])]
        reminders = [reminder for reminder in reminders if reminder]
        if reminders:
            extra["reminders"] = reminders

        parent = self.ctx.parents.get(self.id)
        return Issue(
            source_ref=self.ref,
            project_ref=ctx.project_ref(self.queue),
            tracker_ref=TRACKER_REF,
            status_ref=status_ref(status) if status.lower() in ctx.statuses else status_ref("open"),
            priority_ref=priority_ref(label),
            subject=str(t.get("subject") or ""),
            author_ref=ctx.user_ref(t.get("creator")),
            assigned_to_ref=ctx.user_ref(t.get("owner")),
            description=description,
            parent_ref=str(parent) if parent is not None else None,
            start_date=local_date(rt_time(t.get("starts")), ctx.zone),
            due_date=local_date(rt_time(t.get("due")), ctx.zone),
            estimated_hours=hours(int(t["timeestimated"])) if t.get("timeestimated") else None,
            remaining_estimate=hours(int(t["timeleft"])) if t.get("timeleft") and int(t["timeleft"]) > 0 else None,
            custom_values=self._custom_values(),
            created_at=created,
            updated_at=rt_time(t.get("lastupdated")) or created,
            closed_at=resolved if ctx.lifecycles.is_inactive(lifecycle, status) else None,
            extra={key: value for key, value in extra.items() if value is not None},
        )

    def _reminder(self, reminder_id: int) -> dict[str, Any] | None:
        row = self.batch.reminders.get(reminder_id)
        if row is None:
            return None
        reminder: dict[str, Any] = {
            "rt_ticket": reminder_id,
            "subject": row.get("subject"),
            "status": row.get("status"),
        }
        owner = self.ctx.users.get(int(row.get("owner") or 0))
        if owner:
            reminder["owner"] = owner
        due = rt_time(row.get("due"))
        if due:
            reminder["due"] = iso(due)
        return reminder

    def _description(self, create: Mapping[str, Any] | None) -> tuple[str | None, Journal | None]:
        """The issue's description, and the journal holding the first message when RT 6.0 has a description too."""
        message_text: str | None = None
        message: MailMessage | None = None
        if create is not None:
            message = self._message(create)
            message_text = self._body(message) if message else None
        own = str(self.ticket.get("description") or "").strip()
        if not own:
            return message_text, None
        description = self._text(own, html=True, message=None)
        if not message_text:
            return description, None
        assert create is not None
        journal = Journal(
            source_ref=str(create["id"]),
            issue_ref=self.ref,
            user_ref=self.ctx.user_ref(create.get("creator")),
            notes=message_text,
            created_at=rt_time(create.get("created")),
            extra={
                "rt_transaction": int(create["id"]),
                "rt_type": "Create",
                **(message.mail_headers() if message else {}),
            },
        )
        return description, journal

    # -- messages --------------------------------------------------------

    def _message(self, txn: Mapping[str, Any]) -> MailMessage | None:
        """The message of a transaction, with its files written to the bundle."""
        tops = self.batch.parts.get(int(txn["id"]))
        if not tops:
            return None
        message = take_apart(tops[0], self.ctx.storage)
        for extra_top in tops[1:]:
            more = take_apart(extra_top, self.ctx.storage)
            message.files.extend(more.files)
        for part, problem in message.unreadable:
            self._unreadable(part, problem)
        message.files = [part for part in message.files if self._file(part, txn)]
        return message

    def _file(self, part: Part, txn: Mapping[str, Any]) -> bool:
        if txn.get("type") == "Comment" and not self.ctx.include_comment_files:
            # The note is private, but a file on the issue would be seen by everyone who sees the issue.
            self.ctx.count("files of private comments (--include-comment-files carries them)", 1)
            return False
        try:
            data = part.data(self.ctx.storage)
        except ContentUnavailableError as problem:
            self._unreadable(part, problem)
            return False
        stored = self.writer.add_file(data)
        self.ctx.attachment_bytes += stored.size
        extra: dict[str, Any] = {"rt_transaction": int(txn["id"])}
        if part.content_id:
            extra["content_id"] = part.content_id
        self.writer.add(
            Attachment(
                source_ref=str(part.id),
                container_kind="issue",
                container_ref=self.ref,
                filename=part.display_filename(),
                sha256=stored.sha256,
                filesize=stored.size,
                content_type=part.content_type,
                author_ref=self.ctx.user_ref(txn.get("creator")),
                created_at=rt_time(txn.get("created")),
                extra=extra,
            )
        )
        self.files[part.id] = str(part.id)
        return True

    def _unreadable(self, part: Part, problem: ContentUnavailableError) -> None:
        if problem.external and "no --external-storage-path" in problem.reason:
            self.ctx.count("files in RT's external storage, not read (give --external-storage-path for Disk)", 1)
        elif problem.external:
            self.ctx.count("files missing from RT's external storage", 1)
        else:
            self.ctx.count(f"files that could not be decoded ({problem.reason})", 1)

    def _body(self, message: MailMessage) -> str | None:
        if message.body is None:
            return None
        return self._text(message.body.text, html=message.body.html, message=message) or None

    def _text(self, text: str, *, html: bool, message: MailMessage | None) -> str:
        cids = {part.content_id: str(part.id) for part in message.files if part.content_id} if message else {}

        def attachment_ref(target: str) -> str | None:
            if target in cids:
                return cids[target]
            if target.isdigit() and int(target) in self.files:
                return self.files[int(target)]
            return None

        markdown = html_to_markdown(text, attachment_ref) if html else plain_to_markdown(text)
        return link_references(markdown, self.owner.index, hash_numbers=self.ctx.link_hash_numbers)

    # -- custom fields ---------------------------------------------------

    def _custom_values(self) -> list[CustomValue]:
        by_field: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.batch.values.get(self.id, []):
            if int(row.get("disabled") or 0):
                continue
            by_field[int(row["customfield"])].append(row)
        values: list[CustomValue] = []
        for field_id, rows in sorted(by_field.items()):
            info = self.ctx.fields.get(field_id)
            if info is None or info.is_file:
                continue
            if field_id not in self.ctx.carried_fields:
                self.ctx.count("custom field values of fields not applied to an exported queue", len(rows))
                continue
            texts: list[str] = []
            for row in sorted(rows, key=lambda row: (int(row.get("sortorder") or 0), int(row["id"]))):
                try:
                    texts.append(cf.value_text(info, row, self.ctx.storage))
                except ContentUnavailableError:
                    self.ctx.count("custom field values in external storage that could not be read", 1)
            if info.rt_type == "Select" and info.choices:
                texts = [text for text in texts if text in info.choices]
            value = cf.scalar(info, texts)
            if value is None or value == []:
                continue
            values.append(CustomValue(field_ref=info.ref, key=info.key, value=value))
        return values

    def _custom_file_values(self) -> None:
        """Image and Binary custom field values: files on the issue."""
        for row in self.batch.values.get(self.id, []):
            info = self.ctx.fields.get(int(row["customfield"]))
            if info is None or not info.is_file or int(row.get("disabled") or 0):
                continue
            try:
                filename, data, content_type = cf.file_value(row, self.ctx.storage)
            except ContentUnavailableError as problem:
                self.ctx.count(
                    "custom field files in RT's external storage, not read"
                    if problem.external
                    else "custom field files that could not be decoded",
                    1,
                )
                continue
            stored = self.writer.add_file(data)
            self.ctx.attachment_bytes += stored.size
            self.writer.add(
                Attachment(
                    source_ref=f"cf-value:{row['id']}",
                    container_kind="issue",
                    container_ref=self.ref,
                    filename=filename[:255],
                    sha256=stored.sha256,
                    filesize=stored.size,
                    content_type=content_type,
                    description=f"{info.name} (custom field)"[:512],
                    author_ref=self.ctx.user_ref(row.get("creator")),
                    created_at=rt_time(row.get("created")),
                    extra={"rt_custom_field": info.id, "rt_custom_field_name": info.name},
                )
            )

    # -- watchers --------------------------------------------------------

    def _roles(self) -> tuple[list[int], dict[str, list[str]]]:
        """The carried users in each of the ticket's roles but Owner (groups in a role give their users)."""
        watchers: set[int] = set()
        people: dict[str, list[str]] = {}
        for role, members in sorted(self.batch.roles.get(self.id, {}).items()):
            if role == "Owner":
                continue
            users = [member for member in members if member in self.ctx.users]
            if users:
                watchers.update(users)
                people[self._role_name(role)] = [self.ctx.users[user] for user in users]
        return sorted(watchers), people

    def _role_name(self, role: str) -> str:
        if role.startswith("RT::CustomRole-"):
            try:
                return self.ctx.custom_roles.get(int(role.rsplit("-", 1)[1]), role)
            except ValueError:
                return role
        return role

    # -- history ---------------------------------------------------------

    def _history(self, transactions: list[dict[str, Any]]) -> None:
        group: list[tuple[dict[str, Any], list[JournalDetail], str | None, dict[str, Any]]] = []

        def flush() -> None:
            if not group:
                return
            first = group[0][0]
            details = [detail for _, found, _, _ in group for detail in found]
            notes = next((text for _, _, text, _ in group if text), None)
            private = any(txn["type"] == "Comment" for txn, _, _, _ in group)
            extra: dict[str, Any] = {"rt_transactions": [int(txn["id"]) for txn, _, _, _ in group]}
            for _, _, _, more in group:
                extra.update(more)
            self.writer.add(
                Journal(
                    source_ref=str(first["id"]),
                    issue_ref=self.ref,
                    user_ref=self.ctx.user_ref(first.get("creator")),
                    notes=notes,
                    is_private=private,
                    created_at=rt_time(first.get("created")),
                    details=details,
                    extra=extra,
                )
            )
            group.clear()

        for txn in transactions:
            self._time_entry(txn)
            result = self._transaction(txn)
            if result is None:
                continue
            details, notes, extra = result
            if not details and not notes:
                continue
            if group:
                first = group[0][0]
                same = (
                    first.get("creator") == txn.get("creator")
                    and first.get("created") == txn.get("created")
                    and first["objectid"] == txn["objectid"]
                    # A comment is private as a whole: never with changes or a reply in one entry.
                    and (first["type"] == "Comment") == (txn["type"] == "Comment")
                )
                has_notes = any(text for _, _, text, _ in group)
                if not same or (notes and has_notes):
                    flush()
            group.append((txn, details, notes, extra))
        flush()

    def _transaction(self, txn: dict[str, Any]) -> tuple[list[JournalDetail], str | None, dict[str, Any]] | None:
        kind = str(txn.get("type") or "")
        field_name = str(txn.get("field") or "")
        object_id = int(txn["objectid"])
        extra: dict[str, Any] = {}
        if object_id != self.id:
            extra["merged_from"] = object_id

        if object_id != self.id and kind not in (*MESSAGE_TYPES, "Create", "AddLink", "DeleteLink"):
            # A merged ticket's own status, owner, watchers and fields were its own, not the
            # survivor's: replaying them would rewrite the survivor's history. Only its messages,
            # time and links are folded.
            if kind in NOT_CARRIED_TYPES:
                self.ctx.count(NOT_CARRIED_TYPES[kind], 1)
            elif not (kind == "Set" and field_name in ("TimeWorked", "TimeLeft")):
                self.ctx.count("field changes of merged tickets (not replayed on the ticket they were merged into)", 1)
            return None

        if kind in MESSAGE_TYPES or (kind == "Create" and object_id != self.id):
            message = self._message(txn)
            notes = self._body(message) if message else None
            details = [
                JournalDetail(property="attachment", prop_key=str(part.id), new_value=part.display_filename())
                for part in (message.files if message else [])
            ]
            if message:
                extra.update(message.mail_headers())
            if kind == "Create":
                extra["merged_ticket_created"] = object_id
            return details, notes, extra

        old, new = self._values(txn)
        if kind == "Status" or (kind == "Set" and field_name == "Status"):
            return [self._status(old, new)], None, extra
        if kind in ("Give", "Take", "Steal", "Untake", "Force") or (kind == "Set" and field_name == "Owner"):
            return [self._user_detail("assigned_to_id", old, new)], None, extra
        if kind == "SetWatcher" and field_name == "Owner":
            return None  # The Set of Owner beside it says it already.
        if kind == "Set":
            return self._set(field_name, old, new), None, extra
        if kind in ("AddWatcher", "DelWatcher", "SetWatcher"):
            role = self._role_name(field_name).lower()
            return [JournalDetail(property="attr", prop_key=role, old_value=self._principal(old),
                                  new_value=self._principal(new))], None, extra  # fmt: skip
        if kind in ("AddLink", "DeleteLink"):
            detail = self._link(field_name, old, new, object_id)
            return ([detail] if detail else []), None, extra
        if kind == "CustomField":
            detail = self._custom_field(txn)
            return ([detail] if detail else []), None, extra
        if kind in NOT_CARRIED_TYPES:
            self.ctx.count(NOT_CARRIED_TYPES[kind], 1)
            return None
        self.ctx.count(f"transactions of type {kind or '(none)'}", 1)
        return None

    def _values(self, txn: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """A transaction's old and new values, from ObjectContents when RT 6.0 kept them apart."""
        long_value = self.batch.long_values.get(int(txn["id"]))
        if long_value is not None:
            return _string(long_value.get("OldValue")), _string(long_value.get("NewValue"))
        return _string(txn.get("oldvalue")), _string(txn.get("newvalue"))

    def _status(self, old: str | None, new: str | None) -> JournalDetail:
        return JournalDetail(
            property="attr",
            prop_key="status_id",
            old_value=old,
            new_value=new,
            old_ref=status_ref(old) if old else None,
            new_ref=status_ref(new) if new else None,
        )

    def _user_detail(self, key: str, old: str | None, new: str | None) -> JournalDetail:
        def ref(value: str | None) -> str | None:
            return self.ctx.user_ref(int(value)) if value and value.isdigit() else None

        return JournalDetail(
            property="attr",
            prop_key=key,
            old_value=self._principal(old),
            new_value=self._principal(new),
            old_ref=ref(old),
            new_ref=ref(new),
        )

    def _principal(self, value: str | None) -> str | None:
        if value and value.isdigit():
            name = self.ctx.principal_names.get(int(value))
            if name is not None:
                return None if name.lower() == "nobody" else name
        return value

    def _set(self, name: str, old: str | None, new: str | None) -> list[JournalDetail]:
        ctx = self.ctx
        if name == "Queue":
            return [
                JournalDetail(
                    property="attr",
                    prop_key="project_id",
                    old_value=ctx.queue_name(int(old)) if old and old.isdigit() else old,
                    new_value=ctx.queue_name(int(new)) if new and new.isdigit() else new,
                    old_ref=old if old and old.isdigit() and int(old) in ctx.exported_queues else None,
                    new_ref=new if new and new.isdigit() and int(new) in ctx.exported_queues else None,
                )
            ]
        if name == "Priority":
            queue_name = ctx.queue_name(self.queue)
            old_label = ctx.priorities.label(queue_name, _int(old))[0] if old is not None else None
            new_label = ctx.priorities.label(queue_name, _int(new))[0] if new is not None else None
            if old_label == new_label:
                return [JournalDetail(property="attr", prop_key="priority", old_value=old, new_value=new)]
            return [
                JournalDetail(
                    property="attr",
                    prop_key="priority_id",
                    old_value=old_label,
                    new_value=new_label,
                    old_ref=priority_ref(old_label) if old_label else None,
                    new_ref=priority_ref(new_label) if new_label else None,
                )
            ]
        if name == "Subject":
            return [JournalDetail(property="attr", prop_key="subject", old_value=old, new_value=new)]
        if name in ("Due", "Starts"):
            key = "due_date" if name == "Due" else "start_date"
            return [JournalDetail(property="attr", prop_key=key, old_value=self._date(old), new_value=self._date(new))]
        if name == "TimeEstimated":
            return [
                JournalDetail(
                    property="attr",
                    prop_key="estimated_hours",
                    old_value=str(hours(int(old))) if old and old.lstrip("-").isdigit() and int(old) else None,
                    new_value=str(hours(int(new))) if new and new.lstrip("-").isdigit() and int(new) else None,
                )
            ]
        if name in ("TimeWorked", "TimeLeft"):
            # Time worked is carried as time entries; Specivo keeps no history of the remaining estimate.
            return []
        if name == "Description":
            return [
                JournalDetail(
                    property="attr",
                    prop_key="description",
                    old_value=self._text(old, html=True, message=None) if old else None,
                    new_value=self._text(new, html=True, message=None) if new else None,
                )
            ]
        if name in ("Told", "Started", "Resolved", "LastUpdated"):
            return []
        return [JournalDetail(property="attr", prop_key=name.lower()[:255] or "field", old_value=old, new_value=new)]

    def _date(self, value: str | None) -> str | None:
        moment = rt_time(value)
        found = local_date(moment, self.ctx.zone)
        return found.isoformat() if found else None

    def _link(self, name: str, old: str | None, new: str | None, object_id: int) -> JournalDetail | None:
        uri = new or old
        number = ticket_number(uri, self.ctx.organization)
        if name == "MergedInto":
            if object_id == self.id:
                return None
            return JournalDetail(property="relation", prop_key="merged", new_value=f"#{object_id}")
        shown = f"#{number}" if number is not None else uri
        if name == "MemberOf":
            target = self.ctx.issue_target(number) if number is not None else None
            ref = target[0] if target else None
            return JournalDetail(
                property="attr",
                prop_key="parent_id",
                old_value=shown if old else None,
                new_value=shown if new else None,
                old_ref=ref if old else None,
                new_ref=ref if new else None,
            )
        relation = _LINK_HISTORY_NAMES.get(name, name.lower())
        return JournalDetail(
            property="relation",
            prop_key=relation,
            old_value=shown if old else None,
            new_value=shown if new else None,
        )

    def _custom_field(self, txn: Mapping[str, Any]) -> JournalDetail | None:
        field_id = _int(txn.get("field"))
        info = self.ctx.fields.get(field_id) if field_id is not None else None
        if info is None or info.is_file or info.id not in self.ctx.carried_fields:
            self.ctx.count("history of custom fields that are not carried", 1)
            return None

        def text(ref: Any) -> str | None:
            row = self.batch.values_by_id.get(int(ref)) if ref else None
            if row is None:
                return None
            try:
                return cf.value_text(info, row, self.ctx.storage)
            except ContentUnavailableError:
                return None

        old, new = text(txn.get("oldreference")), text(txn.get("newreference"))
        if old is None and new is None:
            old, new = _string(txn.get("oldvalue")), _string(txn.get("newvalue"))
        return JournalDetail(property="cf", prop_key=info.ref, old_value=old, new_value=new)

    # -- time ------------------------------------------------------------

    def _time_entry(self, txn: Mapping[str, Any]) -> None:
        minutes = int(txn.get("timetaken") or 0)
        if minutes == 0:
            return
        if minutes < 0:
            self.ctx.count("negative time corrections (time worked reduced in RT)", 1)
            return
        worker = int(txn.get("timeworker") or 0) or int(txn.get("creator") or 0)
        created = rt_time(txn.get("created"))
        worked_on = txn.get("timeworkeddate")
        if isinstance(worked_on, datetime):
            worked_on = worked_on.date()
        if not isinstance(worked_on, date):
            worked_on = local_date(created, self.ctx.zone)
        if worked_on is None:
            self.ctx.count("time entries without a date", 1)
            return
        self.writer.add(
            TimeEntry(
                source_ref=str(txn["id"]),
                project_ref=self.ctx.project_ref(self.queue),
                hours=hours(minutes),
                spent_on=worked_on,
                issue_ref=self.ref,
                user_ref=self.ctx.user_ref(worker),
                activity_ref=ACTIVITY_REF,
                created_at=created,
                extra={
                    "rt_transaction": int(txn["id"]),
                    "rt_type": txn.get("type"),
                    "minutes": minutes,
                    **({"merged_from": int(txn["objectid"])} if int(txn["objectid"]) != self.id else {}),
                },
            )
        )


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
