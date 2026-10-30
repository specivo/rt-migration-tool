"""The queries: everything the export reads from an RT database.

:class:`RTSource` opens one read-only, repeatable-read transaction and reads
through it, so the export sees the database as it was when it started, even
while RT keeps working. Instance-wide tables (users, groups, queues, rights,
custom fields) are small and read whole; tickets and what hangs off them
(transactions, message parts, custom field values, links, watchers) are read a
batch of tickets at a time, so memory does not grow with the database.

Every query selects only columns the database has (see
:mod:`specivo_rt_migration.schema`) and orders by a key, so the same database
always gives the same rows in the same order.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

from sqlalchemy import Column, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from specivo_rt_migration import db, perl
from specivo_rt_migration.schema import SchemaInfo, present, read_schema

logger = logging.getLogger(__name__)

_TICKET_URI_RE = re.compile(r"^fsck\.com-rt://([^/]+)/ticket/\d+$")
# IN lists are cut into pieces of this size.
_IN_CHUNK = 500
# Transactions whose old and new values are principal ids: owner and watcher changes.
# (A Set names one when its field is Owner.)
_NAMING_PRINCIPALS = frozenset({"Give", "Take", "Steal", "Untake", "Force", "SetWatcher", "AddWatcher", "DelWatcher"})


def chunks(values: Sequence[int], size: int = _IN_CHUNK) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class RTSource:
    """One read of an RT database. Use as an async context manager."""

    def __init__(self, url: str, *, batch_size: int = 1000) -> None:
        self.url = url
        self.batch_size = batch_size
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        self.schema: SchemaInfo
        self.tables: db.Tables
        # What could not be read and was passed over, by kind, for the not-carried report.
        self.unreadable: Counter[str] = Counter()

    async def __aenter__(self) -> RTSource:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        self._engine = db.create_source_engine(self.url)
        self._conn = await self._engine.connect()
        await db.begin_snapshot(self._conn)
        self.schema = await read_schema(self._conn)
        self.tables = db.tables_for(self._conn.dialect.name)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.rollback()
            await self._conn.close()
            self._conn = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @property
    def conn(self) -> AsyncConnection:
        assert self._conn is not None, "not connected"
        return self._conn

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _columns(self, table: str) -> list[Column[Any]]:
        return present(self.tables, self.schema, table)

    async def _all(self, table: str, *where: Any) -> list[dict[str, Any]]:
        """Every row of *table* matching *where*, in id order."""
        t = self.tables.by_name[table]
        stmt = select(*self._columns(table))
        for condition in where:
            stmt = stmt.where(condition)
        return [row async for row in db.stream(self.conn, stmt, t.c.id, self.batch_size)]

    async def _in(self, table: str, column: str, ids: Sequence[int], *where: Any) -> list[dict[str, Any]]:
        """Rows of *table* whose *column* is one of *ids*, in id order."""
        t = self.tables.by_name[table]
        rows: list[dict[str, Any]] = []
        for piece in chunks(sorted(set(ids))):
            stmt = select(*self._columns(table)).where(t.c[column].in_(piece))
            for condition in where:
                stmt = stmt.where(condition)
            rows.extend(dict(row) for row in (await self.conn.execute(stmt.order_by(t.c.id))).mappings())
        rows.sort(key=lambda row: row["id"])
        return rows

    async def count(self, table: str, *where: Any) -> int:
        """How many rows of *table* match *where*; 0 when this RT has no such table."""
        if not self.schema.has_table(table):
            return 0
        t = self.tables.by_name[table]
        stmt = select(func.count()).select_from(t)
        for condition in where:
            stmt = stmt.where(condition)
        return int((await self.conn.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def setting(self, name: str) -> Any:
        """A setting changed in RT's web interface (RT 5.0 and later), or ``None``."""
        if not self.schema.configurations:
            return None
        t = self.tables.Configurations
        stmt = (
            select(t.c.content, t.c.contenttype)
            .where(t.c.name == name, t.c.disabled == 0)
            .order_by(t.c.id.desc())
            .limit(1)
        )
        row = (await self.conn.execute(stmt)).first()
        if row is None:
            return None
        try:
            return perl.parse_setting(row.content, row.contenttype)
        except ValueError as exc:  # JSONDecodeError and PerlDataError are ValueErrors
            logger.warning("RT's setting %s could not be read (%s); RT's default is used", name, exc)
            self.unreadable["settings in RT's database that could not be read (RT's defaults used)"] += 1
            return None

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------

    async def users(self) -> list[dict[str, Any]]:
        """Every user, with ``disabled`` from its principal."""
        users = await self._all("Users")
        principals = {row["id"]: row for row in await self._all("Principals")}
        for user in users:
            principal = principals.get(user["id"])
            user["disabled"] = bool(principal and principal["disabled"])
        return users

    async def groups(self, *domains: str) -> list[dict[str, Any]]:
        t = self.tables.Groups
        return await self._all("Groups", func.lower(t.c.domain).in_([domain.lower() for domain in domains]))

    async def group_members(self, group_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Direct members of *group_ids*."""
        return await self._in("GroupMembers", "groupid", group_ids)

    async def cached_members(self, group_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Every principal in *group_ids*, directly or through other groups (RT's own cache)."""
        t = self.tables.CachedGroupMembers
        return await self._in("CachedGroupMembers", "groupid", group_ids, t.c.disabled == 0)

    async def principal_types(self) -> dict[int, str]:
        return {row["id"]: str(row["principaltype"] or "") for row in await self._all("Principals")}

    async def acl(self) -> list[dict[str, Any]]:
        return await self._all("ACL")

    # ------------------------------------------------------------------
    # Queues, custom fields, roles
    # ------------------------------------------------------------------

    async def queues(self) -> list[dict[str, Any]]:
        return await self._all("Queues")

    async def custom_fields(self) -> list[dict[str, Any]]:
        return await self._all("CustomFields")

    async def object_custom_fields(self) -> list[dict[str, Any]]:
        return await self._all("ObjectCustomFields")

    async def custom_field_values(self) -> list[dict[str, Any]]:
        return await self._all("CustomFieldValues")

    async def custom_roles(self) -> list[dict[str, Any]]:
        return await self._all("CustomRoles")

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    async def ticket_index(self) -> list[dict[str, Any]]:
        """Every ticket's id, effective id, queue, type and status: what references need."""
        t = self.tables.Tickets
        stmt = select(t.c.id, t.c.effectiveid, t.c.queue, t.c.type, t.c.status)
        return [row async for row in db.stream(self.conn, stmt, t.c.id, max(self.batch_size, 5000))]

    async def ticket_people(self, ticket_ids: Sequence[int]) -> set[int]:
        """Every principal *ticket_ids* name: creators, owners, members of their roles, transaction
        authors and time workers, and whoever their owner and watcher changes name."""
        t = self.tables.Tickets
        tx = self.tables.Transactions
        found: set[int] = set()
        columns = [tx.c.creator, tx.c.type, tx.c.field, tx.c.oldvalue, tx.c.newvalue]
        if self.schema.has_column("Transactions", "timeworker"):
            columns.append(tx.c.timeworker)
        for piece in chunks(sorted(set(ticket_ids))):
            for row in (await self.conn.execute(select(t.c.creator, t.c.owner).where(t.c.id.in_(piece)))).mappings():
                found.update((int(row["creator"] or 0), int(row["owner"] or 0)))
            stmt = select(*columns).where(tx.c.objecttype == "RT::Ticket", tx.c.objectid.in_(piece))
            for row in (await self.conn.execute(stmt)).mappings():
                found.update((int(row["creator"] or 0), int(row.get("timeworker") or 0)))
                if row["type"] in _NAMING_PRINCIPALS or (row["type"] == "Set" and row["field"] == "Owner"):
                    for value in (row["oldvalue"], row["newvalue"]):
                        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
                        if text.isdigit():
                            found.add(int(text))
        role_groups = await self.ticket_roles(ticket_ids)
        found.update(int(row["memberid"]) for row in await self.cached_members([int(row["id"]) for row in role_groups]))
        found.discard(0)
        return found

    async def tickets(self, ids: Sequence[int]) -> list[dict[str, Any]]:
        return await self._in("Tickets", "id", ids)

    async def transactions(self, object_type: str, object_ids: Sequence[int]) -> list[dict[str, Any]]:
        t = self.tables.Transactions
        return await self._in("Transactions", "objectid", object_ids, t.c.objecttype == object_type)

    async def attachments(self, transaction_ids: Sequence[int]) -> list[dict[str, Any]]:
        return await self._in("Attachments", "transactionid", transaction_ids)

    async def object_contents(self, transaction_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """RT 6.0: the long old and new values of *transaction_ids*, by transaction id."""
        if not self.schema.object_contents or not transaction_ids:
            return {}
        t = self.tables.ObjectContents
        rows = await self._in(
            "ObjectContents", "objectid", transaction_ids, t.c.objecttype == "RT::Transaction", t.c.disabled == 0
        )
        found: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                value = perl.parse_setting(
                    row["content"], "application/json" if row["contentencoding"] == "json" else None
                )
            except ValueError as exc:
                logger.warning("The values of transaction %s could not be read (%s)", row["objectid"], exc)
                self.unreadable["old and new values RT 6.0 kept apart that could not be read"] += 1
                continue
            if isinstance(value, dict):
                found[int(row["objectid"])] = value
        return found

    async def custom_values(self, object_type: str, object_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Every custom field value of *object_ids*, current and replaced (``disabled``)."""
        t = self.tables.ObjectCustomFieldValues
        return await self._in("ObjectCustomFieldValues", "objectid", object_ids, t.c.objecttype == object_type)

    async def custom_values_by_id(self, ids: Sequence[int]) -> list[dict[str, Any]]:
        return await self._in("ObjectCustomFieldValues", "id", ids)

    async def links(self, ticket_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Links with either end among *ticket_ids*."""
        t = self.tables.Links
        rows: dict[int, dict[str, Any]] = {}
        for piece in chunks(sorted(set(ticket_ids))):
            stmt = select(*self._columns("Links")).where(or_(t.c.localbase.in_(piece), t.c.localtarget.in_(piece)))
            for row in (await self.conn.execute(stmt)).mappings():
                rows[int(row["id"])] = dict(row)
        return [rows[key] for key in sorted(rows)]

    async def all_links(self) -> AsyncIterator[dict[str, Any]]:
        t = self.tables.Links
        async for row in db.stream(self.conn, select(*self._columns("Links")), t.c.id, self.batch_size):
            yield row

    async def local_uri_host(self) -> str | None:
        """The host of the newest ticket URI RT stored as local (``LocalBase`` or ``LocalTarget`` set).

        RT sets those columns only for a URI naming one of its own tickets, so
        the host is its $Organization (the newest link, in case it changed).
        """
        t = self.tables.Links
        for uri, local in ((t.c.base, t.c.localbase), (t.c.target, t.c.localtarget)):
            stmt = select(uri).where(local > 0).order_by(t.c.id.desc()).limit(1)
            value = (await self.conn.execute(stmt)).scalar()
            match = _TICKET_URI_RE.match(str(value or "").strip())
            if match:
                return match.group(1)
        return None

    async def ticket_roles(self, ticket_ids: Sequence[int]) -> list[dict[str, Any]]:
        """The role groups (Requestor, Cc, AdminCc, Owner, custom roles) of *ticket_ids*."""
        t = self.tables.Groups
        return await self._in("Groups", "instance", ticket_ids, func.lower(t.c.domain) == "rt::ticket-role")

    # ------------------------------------------------------------------
    # Articles
    # ------------------------------------------------------------------

    async def classes(self) -> list[dict[str, Any]]:
        return await self._all("Classes")

    async def object_classes(self) -> list[dict[str, Any]]:
        return await self._all("ObjectClasses")

    async def articles(self) -> list[dict[str, Any]]:
        return await self._all("Articles")

    # ------------------------------------------------------------------
    # Counts of what is not carried
    # ------------------------------------------------------------------

    async def count_attributes(self, *names: str) -> int:
        t = self.tables.Attributes
        return await self.count("Attributes", t.c.name.in_(names))

    async def count_custom_values_of(self, lookup_types: Sequence[str]) -> int:
        cf = self.tables.CustomFields
        ocfv = self.tables.ObjectCustomFieldValues
        stmt = (
            select(func.count())
            .select_from(ocfv.join(cf, ocfv.c.customfield == cf.c.id))
            .where(and_(cf.c.lookuptype.in_(lookup_types), ocfv.c.disabled == 0))
        )
        return int((await self.conn.execute(stmt)).scalar_one())
