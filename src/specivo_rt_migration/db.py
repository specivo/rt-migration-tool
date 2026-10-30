"""Read-only access to a Request Tracker database.

RT runs on PostgreSQL and on MySQL or MariaDB with the same tables, created
from its own ``etc/schema.*`` files. The names differ in case only: RT's
schema writes ``Tickets`` and ``EffectiveId``, which PostgreSQL folds to lower
case, while MySQL keeps a table's name as written (and matches column names
in any case). So the tables are declared once, with lower-case column names,
and :func:`tables_for` names each table the way the database at hand spells
it.

The tables are declared by hand rather than reflected, so a unit test needs no
RT, and only the columns this tool reads are declared. The columns that exist
in one RT version and not another are listed in ``docs/versions.md``;
:mod:`specivo_rt_migration.schema` finds out which version a database is
before anything reads them.

Nothing here writes. The tool is pointed at somebody's live tracker, so the
connection exists only to SELECT, inside a read-only transaction.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Column, Date, DateTime, Integer, MetaData, Select, String, Table, Text, TypeDecorator, make_url
from sqlalchemy import text as sql_text
from sqlalchemy.engine import URL, Dialect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

# Async drivers this tool knows how to load. A URL naming a sync driver, or
# none, is given its async equivalent: an operator copying the settings out of
# RT_SiteConfig.pm has no reason to know about drivers.
_ASYNC_DRIVERS: dict[str, str] = {
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
    "pg": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
    "postgresql+psycopg": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mariadb": "mysql+aiomysql",
    "mysql+pymysql": "mysql+aiomysql",
    "mysql+mysqldb": "mysql+aiomysql",
    "mariadb+pymysql": "mysql+aiomysql",
}

# Comfortably inside MySQL's default eight-hour idle timeout.
_MYSQL_POOL_RECYCLE_SECONDS = 3600


class RawValue(TypeDecorator[Any]):
    """A column handed over exactly as the driver returns it.

    RT keeps attachment and custom field content in ``text`` on PostgreSQL
    (binary content base64-encoded) and in ``LONGBLOB`` on MySQL (raw bytes),
    so the same column is a ``str`` on one and ``bytes`` on the other.
    :mod:`specivo_rt_migration.content` decodes both.
    """

    impl = Text
    cache_ok = True

    def result_processor(self, dialect: Dialect, coltype: object) -> Callable[[Any], Any] | None:
        return None


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

# Each table: its name as RT's schema writes it, and its columns. Columns that
# only some RT versions have are read only when the schema has them (see
# schema.SchemaInfo).
_TABLES: dict[str, list[Column[Any]]] = {}


def _table(name: str, *columns: Column[Any]) -> None:
    _TABLES[name] = list(columns)


def _id() -> Column[Any]:
    return Column("id", Integer, primary_key=True)


def _audit() -> list[Column[Any]]:
    return [
        Column("creator", Integer),
        Column("created", DateTime),
        Column("lastupdatedby", Integer),
        Column("lastupdated", DateTime),
    ]


_table(
    "Queues",
    _id(),
    Column("name", String),
    Column("description", String),
    Column("correspondaddress", String),
    Column("commentaddress", String),
    Column("lifecycle", String),
    Column("subjecttag", String),
    Column("sladisabled", Integer),
    Column("disabled", Integer),
    *_audit(),
)

_table(
    "Tickets",
    _id(),
    Column("effectiveid", Integer),
    Column("ismerged", Integer),
    Column("queue", Integer),
    Column("type", String),
    Column("owner", Integer),
    Column("subject", String),
    # RT 6.0 and later: an HTML description of its own.
    Column("description", Text),
    Column("initialpriority", Integer),
    Column("finalpriority", Integer),
    Column("priority", Integer),
    Column("timeestimated", Integer),
    Column("timeworked", Integer),
    Column("status", String),
    Column("sla", String),
    Column("timeleft", Integer),
    Column("told", DateTime),
    Column("starts", DateTime),
    Column("started", DateTime),
    Column("due", DateTime),
    Column("resolved", DateTime),
    *_audit(),
)

_table(
    "Transactions",
    _id(),
    Column("objecttype", String),
    Column("objectid", Integer),
    Column("timetaken", Integer),
    # RT 6.0 and later: who did the work and on which day.
    Column("timeworker", Integer),
    Column("timeworkeddate", Date),
    Column("type", String),
    Column("field", String),
    Column("oldvalue", String),
    Column("newvalue", String),
    Column("referencetype", String),
    Column("oldreference", Integer),
    Column("newreference", Integer),
    Column("data", String),
    Column("creator", Integer),
    Column("created", DateTime),
)

_table(
    "Attachments",
    _id(),
    Column("transactionid", Integer),
    Column("parent", Integer),
    Column("messageid", String),
    Column("subject", String),
    Column("filename", String),
    Column("contenttype", String),
    Column("contentencoding", String),
    Column("content", RawValue),
    Column("headers", Text),
    Column("creator", Integer),
    Column("created", DateTime),
)

_table(
    "Links",
    _id(),
    Column("base", String),
    Column("target", String),
    Column("type", String),
    Column("localtarget", Integer),
    Column("localbase", Integer),
    Column("creator", Integer),
    Column("created", DateTime),
)

_table("Principals", _id(), Column("principaltype", String), Column("disabled", Integer))

_table(
    "Users",
    _id(),
    Column("name", String),
    Column("emailaddress", String),
    Column("realname", String),
    Column("nickname", String),
    Column("organization", String),
    Column("lang", String),
    Column("timezone", String),
    Column("comments", Text),
    *_audit(),
)

_table(
    "Groups",
    _id(),
    Column("name", String),
    Column("description", String),
    Column("domain", String),
    Column("instance", Integer),
    *_audit(),
)

_table("GroupMembers", _id(), Column("groupid", Integer), Column("memberid", Integer))

_table(
    "CachedGroupMembers",
    _id(),
    Column("groupid", Integer),
    Column("memberid", Integer),
    Column("via", Integer),
    Column("immediateparentid", Integer),
    Column("disabled", Integer),
)

_table(
    "ACL",
    _id(),
    Column("principaltype", String),
    Column("principalid", Integer),
    Column("rightname", String),
    Column("objecttype", String),
    Column("objectid", Integer),
)

_table(
    "CustomFields",
    _id(),
    Column("name", String),
    Column("type", String),
    Column("rendertype", String),
    Column("maxvalues", Integer),
    Column("pattern", String),
    Column("lookuptype", String),
    Column("description", String),
    Column("sortorder", Integer),
    Column("disabled", Integer),
    *_audit(),
)

_table(
    "ObjectCustomFields",
    _id(),
    Column("customfield", Integer),
    Column("objectid", Integer),
    Column("sortorder", Integer),
)

_table(
    "CustomFieldValues",
    _id(),
    Column("customfield", Integer),
    Column("name", String),
    Column("description", String),
    Column("sortorder", Integer),
    Column("category", String),
)

_table(
    "ObjectCustomFieldValues",
    _id(),
    Column("customfield", Integer),
    Column("objecttype", String),
    Column("objectid", Integer),
    Column("sortorder", Integer),
    Column("content", String),
    Column("largecontent", RawValue),
    Column("contenttype", String),
    Column("contentencoding", String),
    Column("disabled", Integer),
    *_audit(),
)

_table("CustomRoles", _id(), Column("name", String), Column("maxvalues", Integer), Column("disabled", Integer))

_table(
    "Classes",
    _id(),
    Column("name", String),
    Column("description", String),
    Column("sortorder", Integer),
    Column("disabled", Integer),
    *_audit(),
)

_table(
    "Articles",
    _id(),
    Column("name", String),
    Column("summary", String),
    Column("sortorder", Integer),
    Column("class", Integer),
    Column("parent", Integer),
    Column("uri", String),
    Column("disabled", Integer),
    *_audit(),
)

_table("ObjectClasses", _id(), Column("class", Integer), Column("objecttype", String), Column("objectid", Integer))
_table("Topics", _id(), Column("parent", Integer), Column("name", String))
_table("ObjectTopics", _id(), Column("topic", Integer), Column("objecttype", String), Column("objectid", Integer))

_table(
    "Attributes",
    _id(),
    Column("name", String),
    Column("description", String),
    Column("content", RawValue),
    Column("contenttype", String),
    Column("objecttype", String),
    Column("objectid", Integer),
)

# RT 5.0 and later: settings changed through the web interface.
_table(
    "Configurations",
    _id(),
    Column("name", String),
    # A BLOB on MySQL.
    Column("content", RawValue),
    Column("contenttype", String),
    Column("disabled", Integer),
)

# RT 6.0 and later: values too long for a transaction's OldValue/NewValue.
_table(
    "ObjectContents",
    _id(),
    Column("objecttype", String),
    Column("objectid", Integer),
    Column("contentencoding", String),
    Column("content", Text),
    Column("disabled", Integer),
)

# Only counted, never read: what the bundle has no place for.
_table("Assets", _id())
_table("Catalogs", _id())
_table("Scrips", _id(), Column("disabled", Integer))
_table("Templates", _id())
_table("SavedSearches", _id(), Column("disabled", Integer))
_table("Dashboards", _id(), Column("disabled", Integer))

TABLE_NAMES: tuple[str, ...] = tuple(_TABLES)


@dataclass(frozen=True)
class Tables:
    """RT's tables, named as one database spells them. ``tables.Tickets`` and so on."""

    metadata: MetaData
    by_name: dict[str, Table]

    def __getattr__(self, name: str) -> Table:
        try:
            return self.by_name[name]
        except KeyError:
            raise AttributeError(name) from None


def tables_for(dialect_name: str) -> Tables:
    """Declare RT's tables as *dialect_name* names them: lower case except on MySQL."""
    metadata = MetaData()
    by_name: dict[str, Table] = {}
    for name, columns in _TABLES.items():
        physical = name if dialect_name == "mysql" else name.lower()
        by_name[name] = Table(physical, metadata, *(column._copy() for column in columns))
    return Tables(metadata=metadata, by_name=by_name)


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


def normalise_source_url(url: str | URL) -> URL:
    """Return *url* with an async driver, leaving an explicit async one alone.

    A :class:`~sqlalchemy.engine.URL` is returned rather than a string on
    purpose: ``str(URL)`` masks the password, so a round trip through a string
    would replace the credential with asterisks.
    """
    parsed = make_url(url)
    replacement = _ASYNC_DRIVERS.get(parsed.drivername)
    if replacement is None:
        return parsed
    return parsed.set(drivername=replacement)


class SourceURLError(ValueError):
    """The database URL is not one this tool can read. The message never holds the password."""


# The databases RT runs on, and SQLite, which the tool's tests use.
_SUPPORTED_BACKENDS: dict[str, str] = {"postgresql": "asyncpg", "mysql": "aiomysql", "sqlite": "aiosqlite"}

# The password of a URL, found without parsing it: for messages about a URL
# that does not parse.
_URL_PASSWORD_RE = re.compile(r"(?P<head>://[^/:@\s]*:)(?P<password>[^@\s]*)(?P<tail>@)")


def check_source_url(url: str) -> URL:
    """Return *url* parsed and given its async driver, or raise :class:`SourceURLError`."""
    try:
        parsed = normalise_source_url(url)
        _ = parsed.port
    except Exception:  # noqa: BLE001 - make_url raises several kinds
        raise SourceURLError(
            "is not a database URL; expected postgresql://USER:PASSWORD@HOST[:PORT]/DATABASE "
            "or mysql://USER:PASSWORD@HOST[:PORT]/DATABASE"
        ) from None
    backend = parsed.get_backend_name()
    if backend not in _SUPPORTED_BACKENDS:
        raise SourceURLError(
            f"names a {backend} database ({safe_url(parsed)}); this tool reads RT on PostgreSQL, MySQL or MariaDB"
        )
    expected = _SUPPORTED_BACKENDS[backend]
    if parsed.get_driver_name() != expected:
        raise SourceURLError(f"asks for the {parsed.get_driver_name()} driver ({safe_url(parsed)}); use {backend}://")
    if not parsed.database:
        raise SourceURLError(f"names no database ({safe_url(parsed)})")
    return parsed


def mask_password(text: str, url: str) -> str:
    """Return *text* with the password of *url* masked, whether or not *url* parses. Never raises."""
    passwords: set[str] = set()
    try:
        password = make_url(url).password
        if password:
            passwords.add(str(password))
    except Exception:  # noqa: BLE001 - an unparsable URL still has a password to hide
        pass
    for match in _URL_PASSWORD_RE.finditer(url or ""):
        if match.group("password"):
            passwords.add(match.group("password"))
    for password in sorted(passwords, key=len, reverse=True):
        text = text.replace(password, "***")
    return _URL_PASSWORD_RE.sub(r"\g<head>***\g<tail>", text)


def safe_url(url: str | URL) -> str:
    """Return *url* with the password masked, for messages."""
    return make_url(url).render_as_string(hide_password=True)


class SourceDriverMissingError(RuntimeError):
    """The driver for the source database is not installed."""


def create_source_engine(url: str | URL, **engine_kwargs: Any) -> AsyncEngine:
    """Return an engine for the RT database at *url*.

    An export can run for hours, and a database may drop an idle connection
    in that time. On PostgreSQL connections are pinged before use; on MySQL
    they are recycled by age instead, because SQLAlchemy's aiomysql adapter
    cannot ping.
    """
    normalised = normalise_source_url(url)
    driver = normalised.get_driver_name()
    if driver in ("asyncpg", "aiomysql") and importlib.util.find_spec(driver) is None:
        raise SourceDriverMissingError(f"The {driver} driver is needed to read this database: pip install {driver}")
    if normalised.get_backend_name() == "mysql":
        engine_kwargs.setdefault("pool_recycle", _MYSQL_POOL_RECYCLE_SECONDS)
        # RT's MySQL schema is utf8mb4; ask for it, or Thai text arrives mangled.
        normalised = normalised.update_query_dict({"charset": "utf8mb4"})
    elif normalised.get_backend_name() == "postgresql":
        engine_kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(normalised, **engine_kwargs)


# The statements that open the export's snapshot, by dialect: a read-only,
# repeatable-read transaction, so the session cannot write even with an
# account that could, and every query sees the database as it was when the
# first one ran.
_SNAPSHOT_STATEMENTS: dict[str, tuple[str, ...]] = {
    "postgresql": ("SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ",),
    "mysql": (
        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ),
}


async def begin_snapshot(conn: AsyncConnection) -> None:
    """Start the read-only, repeatable-read transaction the export reads in. Must come first on *conn*."""
    for statement in _SNAPSHOT_STATEMENTS.get(conn.dialect.name, ()):
        await conn.execute(sql_text(statement))


async def stream(
    conn: AsyncConnection, stmt: Select[Any], key_column: Column[Any], batch_size: int = 1000
) -> AsyncIterator[dict[str, Any]]:
    """Yield every row of *stmt* as a dict, a page at a time, in the order of *key_column*.

    Pages by ``key_column > last seen`` rather than OFFSET, which would make
    the database scan a growing prefix for every page. *stmt* must not carry
    its own ordering or limit, and *key_column* must be unique.
    """
    last: Any = None
    while True:
        page = stmt if last is None else stmt.where(key_column > last)
        rows = (await conn.execute(page.order_by(key_column).limit(batch_size))).mappings().all()
        for row in rows:
            yield dict(row)
        if len(rows) < batch_size:
            return
        last = rows[-1][key_column.name]
