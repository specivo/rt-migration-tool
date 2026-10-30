"""Which RT a database belongs to, read from its schema.

RT keeps no version number in its database, but each release line changed the
schema in ways that can be seen from outside (``docs/versions.md`` lists them):

- **4.4** added custom roles (``CustomRoles``), merged-ticket bookkeeping
  (``Tickets.IsMerged``), service levels (``Tickets.SLA``) and assets. Older
  databases are refused.
- **5.0** added settings stored in the database (``Configurations``), auth
  tokens (``AuthTokens``) and the URL shortener (``Shorteners``).
- **6.0** added who logged time and on which day (``Transactions.TimeWorker``,
  ``TimeWorkedDate``), a ticket description of its own (``Tickets.Description``),
  long transaction values kept apart (``ObjectContents``), and saved searches
  and dashboards in tables of their own instead of attributes.

The export reads only the columns the database has, so a 6.0 feature is never
asked of a 4.4 database. A database between two lines (a development release,
an upgrade that stopped half way) is described by the features it has.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Column, inspect
from sqlalchemy.ext.asyncio import AsyncConnection

from specivo_rt_migration.db import TABLE_NAMES, Tables


class UnsupportedSchemaError(Exception):
    """The database is not an RT database this tool can read."""


@dataclass(frozen=True)
class SchemaInfo:
    """What one database holds: its RT line, and every table and column the tool might read."""

    version: str
    # Table name as RT's schema writes it -> lower-case column names it has.
    columns: dict[str, frozenset[str]] = field(default_factory=dict)

    def has_table(self, table: str) -> bool:
        return table in self.columns

    def has_column(self, table: str, column: str) -> bool:
        return column in self.columns.get(table, frozenset())

    @property
    def time_worker(self) -> bool:
        """Transactions say whose time they logged, and on which day (6.0)."""
        return self.has_column("Transactions", "timeworker") and self.has_column("Transactions", "timeworkeddate")

    @property
    def ticket_description(self) -> bool:
        return self.has_column("Tickets", "description")

    @property
    def configurations(self) -> bool:
        return self.has_table("Configurations")

    @property
    def object_contents(self) -> bool:
        return self.has_table("ObjectContents")

    def describe(self) -> str:
        return f"RT {self.version}"


def classify(columns: dict[str, frozenset[str]]) -> str:
    """Return the RT line (``4.4``, ``5.0`` or ``6.0``) a schema belongs to, or raise."""
    if "Tickets" not in columns or "Transactions" not in columns or "Attachments" not in columns:
        raise UnsupportedSchemaError("this database has no RT tables (Tickets, Transactions, Attachments)")
    if "CustomRoles" not in columns or "ismerged" not in columns["Tickets"] or "sla" not in columns["Tickets"]:
        raise UnsupportedSchemaError("this is an RT older than 4.4, which this tool does not read; upgrade RT first")
    six = "timeworker" in columns["Transactions"] and "description" in columns["Tickets"]
    if six or "SavedSearches" in columns or "ObjectContents" in columns:
        return "6.0"
    if "Configurations" in columns:
        return "5.0"
    return "4.4"


async def read_schema(conn: AsyncConnection) -> SchemaInfo:
    """Look at the tables and columns of the database on *conn* and say which RT it is."""

    def inspect_sync(sync_conn: object) -> dict[str, frozenset[str]]:
        inspector = inspect(sync_conn)
        assert inspector is not None
        present = {name.lower(): name for name in inspector.get_table_names()}
        found: dict[str, frozenset[str]] = {}
        for table in TABLE_NAMES:
            physical = present.get(table.lower())
            if physical is None:
                continue
            found[table] = frozenset(column["name"].lower() for column in inspector.get_columns(physical))
        return found

    columns = await conn.run_sync(inspect_sync)
    return SchemaInfo(version=classify(columns), columns=columns)


def present(tables: Tables, schema: SchemaInfo, table: str) -> list[Column[object]]:
    """The declared columns of *table* that this database has, to select only those."""
    declared = tables.by_name[table]
    return [column for column in declared.columns if schema.has_column(table, column.name)]
