"""A small RT database in SQLite, for tests that run the whole export.

The tables are the tool's own declarations (``specivo_rt_migration.db``), named
as PostgreSQL names them, so the rows below run through the real queries
without an RT. :func:`build` leaves out the tables and columns an older RT
does not have, so the same rows serve RT 4.4, 5.0 and 6.0. The values are
shaped like an RT database: naive UTC timestamps, ``1970-01-01`` for an unset
date, role groups per ticket, padded IP addresses, message parts linked by
``Parent``, content base64-encoded or quoted-printable, a file in Disk external
storage.

The end-to-end test against a real RT is ``test_e2e_fixture.py``.
"""

from __future__ import annotations

import base64
import hashlib
import quopri
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy import insert as insert_

from specivo_rt_migration import db

T0 = datetime(2026, 1, 5, 9, 0, 0)
T1 = datetime(2026, 1, 6, 10, 30, 0)
T2 = datetime(2026, 1, 7, 14, 15, 0)
T3 = datetime(2026, 1, 8, 16, 45, 0)
UNSET = datetime(1970, 1, 1, 0, 0, 0)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 4 + b"\n%%EOF\n"
PDF_SHA = hashlib.sha256(PDF).hexdigest()
LOG = b"session expired\nrenew failed\n"

# Ids.
RT_SYSTEM, NOBODY, ROOT, ALICE, BOB, SOMCHAI, CAROL, DANA = 1, 6, 14, 20, 21, 22, 23, 24
EVERYONE, PRIVILEGED, UNPRIVILEGED = 3, 4, 5
SUPPORT_TEAM, THAI_DESK = 40, 41
GENERAL, SUPPORT, BILLING, OLD, THAI = 1, 3, 4, 5, 6

# What each RT line lacks, of what this tool reads.
_NOT_IN: dict[str, dict[str, set[str]]] = {
    "4.4": {
        "tables": {"Configurations", "ObjectContents", "SavedSearches", "Dashboards"},
        "columns": {"Transactions.timeworker", "Transactions.timeworkeddate", "Tickets.description"},
    },
    "5.0": {
        "tables": {"ObjectContents", "SavedSearches", "Dashboards"},
        "columns": {"Transactions.timeworker", "Transactions.timeworkeddate", "Tickets.description"},
    },
    "6.0": {"tables": set(), "columns": set()},
}


@dataclass(frozen=True)
class SampleRT:
    """Where the sample lives: its database URL and its external storage directory."""

    url: str
    storage: Path
    version: str


def build(tmp_path: Path, *, version: str = "6.0") -> SampleRT:
    """Write the sample database for RT *version* (``4.4``, ``5.0`` or ``6.0``) under *tmp_path*."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "rt.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    missing = _NOT_IN[version]
    declared = db.tables_for("sqlite")
    metadata = MetaData()
    tables: dict[str, Table] = {}
    for name, table in declared.by_name.items():
        if name in missing["tables"]:
            continue
        columns = [column._copy() for column in table.columns if f"{name}.{column.name}" not in missing["columns"]]
        tables[name] = Table(table.name, metadata, *columns)
    metadata.create_all(engine)
    with engine.begin() as conn:
        for table, rows in _rows(version).items():
            if table not in tables:
                continue
            for row in rows:
                values = {key: value for key, value in row.items() if key in tables[table].c}
                conn.execute(insert_(tables[table]).values(**values))
    engine.dispose()

    storage = tmp_path / "external"
    target = storage / PDF_SHA[:3] / PDF_SHA[3:6] / PDF_SHA[6:]
    target.parent.mkdir(parents=True)
    target.write_bytes(PDF)
    return SampleRT(url=f"sqlite+aiosqlite:///{path}", storage=storage, version=version)


def _audit(when: datetime = T0, creator: int = RT_SYSTEM) -> dict[str, Any]:
    return {"creator": creator, "created": when, "lastupdatedby": creator, "lastupdated": when}


def ticket_row(ticket_id: int, queue: int, subject: str, **values: Any) -> dict[str, Any]:
    """A row of ``Tickets``: a new ticket created by Dana at ``T0``, with *values* over that."""
    row = {
        "id": ticket_id,
        "effectiveid": ticket_id,
        "queue": queue,
        "type": "ticket",
        "owner": NOBODY,
        "subject": subject,
        "initialpriority": 0,
        "finalpriority": 0,
        "priority": 0,
        "timeestimated": 0,
        "timeworked": 0,
        "status": "new",
        "timeleft": 0,
        "told": UNSET,
        "starts": UNSET,
        "started": UNSET,
        "due": UNSET,
        "resolved": UNSET,
        **_audit(T0, DANA),
    }
    row.update(values)
    return row


def insert(sample: SampleRT, table: str, *rows: dict[str, Any]) -> None:
    """Add *rows* to *table* of a built sample, for a test that needs a case of its own.

    Columns the sample's RT version does not have are left out, as :func:`build` does.
    """
    engine = create_engine(sample.url.replace("sqlite+aiosqlite", "sqlite"))
    target = Table(db.tables_for("sqlite").by_name[table].name, MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        for row in rows:
            conn.execute(insert_(target).values(**{key: value for key, value in row.items() if key in target.c}))
    engine.dispose()


def update(sample: SampleRT, table: str, row_id: int, **values: Any) -> None:
    """Change the row *row_id* of *table* in a built sample."""
    engine = create_engine(sample.url.replace("sqlite+aiosqlite", "sqlite"))
    target = Table(db.tables_for("sqlite").by_name[table].name, MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(target.update().where(target.c.id == row_id).values(**values))
    engine.dispose()


def _rows(version: str) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}

    def add(table: str, **values: Any) -> None:
        rows.setdefault(table, []).append(values)

    # -- people ---------------------------------------------------------
    people = [
        (RT_SYSTEM, "RT_System", None, "The RT System itself"),
        (NOBODY, "Nobody", None, "Nobody in particular"),
        (ROOT, "root", "root@example.com", "Superuser"),
        (ALICE, "alice", "alice@example.com", "Alice Example"),
        (BOB, "bob", "bob@example.com", "Bob Example"),
        (SOMCHAI, "somchai", "somchai@example.com", "สมชาย ใจดี"),
        (CAROL, "carol", "carol@example.com", "Carol Example"),
        (DANA, "dana@example.net", "dana@example.net", "Dana Customer"),
    ]
    for user_id, name, email, real in people:
        add("Principals", id=user_id, principaltype="User", disabled=1 if user_id == CAROL else 0)
        add(
            "Users",
            id=user_id,
            name=name,
            emailaddress=email,
            realname=real,
            lang="th" if user_id == SOMCHAI else None,
            **_audit(),
        )
        # Each user's ACL equivalence group, which rights to the user are granted to.
        equivalence = 100 + user_id
        add("Principals", id=equivalence, principaltype="Group", disabled=0)
        add("Groups", id=equivalence, name="UserEquiv", domain="ACLEquivalence", instance=user_id, **_audit())
        add("CachedGroupMembers", id=1000 + user_id, groupid=equivalence, memberid=user_id, via=0, disabled=0)

    def group(group_id: int, name: str, domain: str, instance: int = 0, members: tuple[int, ...] = ()) -> None:
        add("Principals", id=group_id, principaltype="Group", disabled=0)
        add("Groups", id=group_id, name=name, domain=domain, instance=instance, **_audit())
        for member in members:
            add("GroupMembers", id=10000 + group_id * 100 + member, groupid=group_id, memberid=member)
            add("CachedGroupMembers", id=20000 + group_id * 100 + member, groupid=group_id, memberid=member, disabled=0)

    group(EVERYONE, "Everyone", "SystemInternal", members=(ROOT, ALICE, BOB, SOMCHAI, CAROL, DANA))
    group(PRIVILEGED, "Privileged", "SystemInternal", members=(ROOT, ALICE, BOB, SOMCHAI, CAROL))
    group(UNPRIVILEGED, "Unprivileged", "SystemInternal", members=(DANA,))
    group(SUPPORT_TEAM, "Support Team", "UserDefined", members=(ALICE, BOB))
    group(THAI_DESK, "Thai Desk", "UserDefined", members=(SOMCHAI,))
    # A nested group: All Agents holds Support Team (and so Alice and Bob) and Somchai.
    group(42, "All Agents", "UserDefined", members=(SUPPORT_TEAM, SOMCHAI))
    for member in (ALICE, BOB):
        add("CachedGroupMembers", id=30000 + member, groupid=42, memberid=member, via=0, disabled=0)
    # Queue role groups: Somchai is an AdminCc of Support.
    group(50, "AdminCc", "RT::Queue-Role", SUPPORT, members=(SOMCHAI,))
    group(51, "Cc", "RT::Queue-Role", SUPPORT)
    group(52, "Requestor", "RT::System-Role", 1)

    acl = [
        ("Group", 100 + ROOT, "SuperUser", "RT::System", 1),
        ("Group", SUPPORT_TEAM, "ShowTicket", "RT::Queue", SUPPORT),
        ("Group", SUPPORT_TEAM, "OwnTicket", "RT::Queue", SUPPORT),
        ("Group", SUPPORT_TEAM, "ModifyTicket", "RT::Queue", BILLING),
        ("Group", 100 + BOB, "AdminQueue", "RT::Queue", BILLING),
        ("Group", THAI_DESK, "ShowTicket", "RT::Queue", THAI),
        ("AdminCc", 50, "ModifyTicket", "RT::Queue", SUPPORT),
        ("Requestor", 52, "ShowTicket", "RT::System", 1),
        ("Group", EVERYONE, "CreateTicket", "RT::System", 1),
        ("Group", 100 + SOMCHAI, "SeeCustomField", "RT::CustomField", 2),
    ]
    for index, (principal_type, principal, right, object_type, object_id) in enumerate(acl, start=1):
        add(
            "ACL",
            id=index,
            principaltype=principal_type,
            principalid=principal,
            rightname=right,
            objecttype=object_type,
            objectid=object_id,
        )

    # -- queues ---------------------------------------------------------
    for queue_id, name, description, disabled, lifecycle in (
        (GENERAL, "General", "The default queue", 0, "default"),
        (2, "___Approvals", "A system-internal queue for the approvals system", 0, "approvals"),
        (SUPPORT, "Support", "Customer support", 0, "default"),
        (BILLING, "Billing", "Invoices and payments", 0, "billing" if version != "4.4" else "default"),
        (OLD, "Old Projects", "Closed down", 1, "default"),
        (THAI, "งานภาษาไทย", "คิวสำหรับลูกค้าภาษาไทย", 0, "default"),
    ):
        add(
            "Queues",
            id=queue_id,
            name=name,
            description=description,
            lifecycle=lifecycle,
            disabled=disabled,
            sladisabled=1,
            correspondaddress="support@example.com" if queue_id == SUPPORT else None,
            **_audit(),
        )

    if version != "4.4":
        add(
            "Configurations",
            id=1,
            name="Lifecycles",
            contenttype="perl",
            disabled=0,
            content="""{
  'billing' => {
                 'active' => ['invoiced', 'overdue'],
                 'defaults' => { 'on_create' => 'quoted' },
                 'inactive' => ['paid', 'written-off'],
                 'initial' => ['quoted']
               },
  'default' => {
                 'active' => ['open', 'stalled'],
                 'inactive' => ['resolved', 'rejected', 'deleted'],
                 'initial' => ['new']
               },
  '__maps__' => {}
}""",
        )
        add(
            "Configurations",
            id=2,
            name="PriorityAsString",
            contenttype="perl",
            disabled=0,
            content=(
                "{ 'Billing' => [ 'Routine', 0, 'Urgent', 70 ], "
                "'Default' => { 'Low' => 0, 'Medium' => 50, 'High' => 100 } }"
            ),
        )

    # -- custom fields --------------------------------------------------
    fields = [
        (2, "Severity", "Select", 1, [SUPPORT]),
        (3, "Components", "Select", 0, [SUPPORT, BILLING]),
        (4, "Customer reference", "Freeform", 1, [0]),
        (5, "Notes", "Text", 1, [SUPPORT]),
        (7, "Go-live date", "Date", 1, [SUPPORT]),
        (8, "Maintenance window", "DateTime", 1, [SUPPORT]),
        (9, "Server address", "IPAddress", 1, [SUPPORT]),
        (10, "Network range", "IPAddressRange", 1, [SUPPORT]),
        (13, "Screenshot", "Image", 1, [SUPPORT]),
        (14, "Contract", "Binary", 1, [BILLING]),
        (15, "ลูกค้า", "Freeform", 1, [THAI]),
    ]
    for field_id, name, kind, max_values, queues in fields:
        add(
            "CustomFields",
            id=field_id,
            name=name,
            type=kind,
            maxvalues=max_values,
            lookuptype="RT::Queue-RT::Ticket",
            sortorder=field_id,
            disabled=0,
            **_audit(),
        )
        for queue in queues:
            add("ObjectCustomFields", id=field_id * 10 + queue, customfield=field_id, objectid=queue, sortorder=0)
    add("CustomFields", id=16, name="Cost centre", type="Freeform", maxvalues=1, lookuptype="RT::Queue", disabled=0)
    add("CustomFields", id=1, name="Content", type="HTML", maxvalues=1, lookuptype="RT::Class-RT::Article", disabled=0)
    for index, name in enumerate(["Low", "High", "Critical"], start=1):
        add("CustomFieldValues", id=index, customfield=2, name=name, sortorder=index)
    for index, name in enumerate(["Web", "Mobile", "API"], start=4):
        add("CustomFieldValues", id=index, customfield=3, name=name, sortorder=index)

    # -- tickets --------------------------------------------------------
    def ticket(ticket_id: int, queue: int, subject: str, **values: Any) -> None:
        add("Tickets", **ticket_row(ticket_id, queue, subject, **values))

    ticket(
        1,
        SUPPORT,
        "Login page is broken",
        owner=BOB,
        status="stalled",
        priority=80,
        initialpriority=20,
        finalpriority=90,
        timeestimated=180,
        timeleft=90,
        timeworked=45,
        due=datetime(2026, 1, 20, 18, 0, 0),
        starts=datetime(2026, 1, 5, 2, 0, 0),
        told=T1,
        lastupdated=T3,
        description="<p>An <b>own</b> description.</p>" if version == "6.0" else None,
    )
    ticket(2, SUPPORT, "Password reset mail is late", owner=BOB, timeworked=65, creator=BOB)
    ticket(3, BILLING, "Invoice for the login fix", creator=BOB, status="quoted" if version != "4.4" else "new")
    ticket(4, SUPPORT, "Duplicate: cannot log in", effectiveid=1, creator=ALICE)
    ticket(5, SUPPORT, "Still cannot log in", effectiveid=1)
    ticket(6, SUPPORT, "Spam", status="deleted", creator=ALICE)
    ticket(7, SUPPORT, "Not our product", status="rejected", resolved=T2, creator=ALICE)
    ticket(8, OLD, "Migrate the old server", status="resolved", resolved=T2, creator=SOMCHAI)
    ticket(9, THAI, "ใบแจ้งหนี้ไม่ถูกต้อง", creator=SOMCHAI)
    ticket(10, BILLING, "Refund for order 1001", creator=BOB, status="paid" if version != "4.4" else "resolved")
    ticket(11, SUPPORT, "Call the customer back", type="reminder", owner=BOB, status="open", due=T3)
    ticket(12, 2, "Approve the refund", type="approval", creator=RT_SYSTEM)
    ticket(13, SUPPORT, "Weekly report", status="custom-status", creator=ALICE)

    # Role groups of tickets.
    role_groups = [
        (60, "Requestor", 1, (DANA,)),
        (61, "Cc", 1, (THAI_DESK,)),
        (62, "AdminCc", 1, (SOMCHAI,)),
        (63, "Owner", 1, (BOB,)),
        (64, "Requestor", 2, ()),
    ]
    for group_id, name, instance, members in role_groups:
        group(group_id, name, "RT::Ticket-Role", instance, members)
    add("CachedGroupMembers", id=40000, groupid=61, memberid=SOMCHAI, via=0, disabled=0)

    # -- transactions and their messages --------------------------------
    txn_id = 0

    def txn(objectid: int, kind: str, when: datetime, creator: int, **values: Any) -> int:
        nonlocal txn_id
        txn_id += 1
        add(
            "Transactions",
            id=txn_id,
            objecttype=values.pop("objecttype", "RT::Ticket"),
            objectid=objectid,
            type=kind,
            timetaken=values.pop("timetaken", 0),
            created=when,
            creator=creator,
            **values,
        )
        return txn_id

    part_id = 0

    def part(transaction: int, parent: int, content_type: str, content: Any = None, **values: Any) -> int:
        nonlocal part_id
        part_id += 1
        add(
            "Attachments",
            id=part_id,
            transactionid=transaction,
            parent=parent,
            contenttype=content_type,
            content=content,
            creator=values.pop("creator", RT_SYSTEM),
            created=values.pop("created", T0),
            **values,
        )
        return part_id

    def headers(content_type: str, **more: str) -> str:
        lines = [f"Content-Type: {content_type}"] + [f"{key.replace('_', '-')}: {value}" for key, value in more.items()]
        return "\n".join(lines) + "\n"

    # Ticket 1 is created by mail: alternative plain and HTML (with an inline
    # image), a PDF in external storage, a reference to ticket 5 (merged).
    create1 = txn(1, "Create", T0, DANA)
    top = part(
        create1,
        0,
        "multipart/mixed",
        "",
        headers=headers(
            'multipart/mixed; boundary="x"',
            From="Dana Customer <dana@example.net>",
            To="support@example.com",
            Cc="eve@example.org",
            Subject="=?UTF-8?B?TG9naW4gcGFnZSBpcyBicm9rZW4=?=",
        ),
        messageid="first-request@example.net",
    )
    alternative = part(create1, top, "multipart/alternative", "")
    part(
        create1,
        alternative,
        "text/plain",
        quopri.encodestring(b"The login page shows an error. See ticket 5.\n").decode(),
        contentencoding="quoted-printable",
        headers=headers('text/plain; charset="utf-8"'),
    )
    related = part(create1, alternative, "multipart/related", "")
    part(
        create1,
        related,
        "text/html",
        '<p>The <b>login page</b> shows an error. See ticket 5.</p><p><img src="cid:shot@example.net" alt="the error">'
        "</p><script>alert(1)</script><p>ภาษาไทย: เข้าสู่ระบบไม่ได้</p>",
        headers=headers('text/html; charset="utf-8"'),
    )
    part(
        create1,
        related,
        "image/png",
        base64.b64encode(PNG).decode(),
        contentencoding="base64",
        filename="error.png",
        headers=headers('image/png; name="error.png"', Content_ID="<shot@example.net>", Content_Disposition="inline"),
    )
    part(
        create1,
        top,
        "application/pdf",
        PDF_SHA,
        contentencoding="external",
        filename="invoice.pdf",
        headers=headers("application/pdf", Content_Disposition='attachment; filename="invoice.pdf"'),
    )

    # Bob takes it, opens it and replies in HTML, logging 15 minutes.
    txn(1, "Set", T1, BOB, field="Owner", oldvalue=str(NOBODY), newvalue=str(BOB))
    txn(1, "SetWatcher", T1, BOB, field="Owner", oldvalue=str(NOBODY), newvalue=str(BOB))
    txn(1, "Status", T1, BOB, field="Status", oldvalue="new", newvalue="open")
    reply = txn(1, "Correspond", T1, BOB, timetaken=15)
    part(
        reply,
        0,
        "text/html",
        "<p>We are <i>on it</i>. [fixture.example.com #2] is related.</p>",
        headers=headers('text/html; charset="utf-8"', From="bob@example.com", To="dana@example.net"),
        creator=BOB,
    )
    # Alice comments privately with a log file, 30 minutes.
    comment = txn(1, "Comment", T2, ALICE, timetaken=30)
    mixed = part(comment, 0, "multipart/mixed", "", headers=headers("multipart/mixed"))
    part(comment, mixed, "text/plain", "Internal: cookie *expires* early.\n# not a heading\nRT #3 too.\n")
    part(comment, mixed, "text/plain", LOG, filename="server.log", headers=headers("text/plain"))
    # Dana answers in Thai, TIS-620 as sent (RT 4.4 kept it so).
    thai = txn(1, "Correspond", T2, DANA)
    part(
        thai,
        0,
        "text/plain",
        base64.b64encode("ขอบคุณครับ".encode("tis-620")).decode(),
        contentencoding="base64",
        headers=headers('text/plain; charset="TIS-620"'),
    )
    # Field changes.
    txn(1, "Set", T3, ALICE, field="Subject", oldvalue="Login page is broken", newvalue="Login page is broken!")
    txn(1, "Set", T3, ALICE, field="Priority", oldvalue="20", newvalue="80")
    txn(1, "Set", T3, ALICE, field="Due", oldvalue="1970-01-01 00:00:00", newvalue="2026-01-20 18:00:00")
    txn(1, "Set", T3, ALICE, field="Queue", oldvalue=str(SUPPORT), newvalue=str(BILLING))
    txn(1, "Set", T3, ALICE, field="Queue", oldvalue=str(BILLING), newvalue=str(SUPPORT))
    txn(1, "AddWatcher", T3, ALICE, field="AdminCc", newvalue=str(SOMCHAI))
    txn(1, "DelWatcher", T3, ALICE, field="Cc", oldvalue=str(DANA))
    txn(1, "Status", T3, ALICE, field="Status", oldvalue="open", newvalue="stalled")
    txn(1, "EmailRecord", T3, RT_SYSTEM)
    txn(1, "Told", T3, RT_SYSTEM)
    # Custom fields, with history: Severity High then Critical.
    values = [
        (1, 2, 1, "High", None, None, None, 1),
        (2, 2, 1, "Critical", None, None, None, 0),
        (3, 3, 1, "Web", None, None, None, 0),
        (4, 3, 1, "API", None, None, None, 1),
        (5, 4, 1, "cust0042", None, None, None, 0),
        (6, 5, 1, "", "First line\nSecond line", "text/plain", "none", 0),
        (7, 7, 1, "2026-12-01", None, None, None, 0),
        (8, 8, 1, "2026-12-01 15:00:00", None, None, None, 0),
        (9, 9, 1, "192.000.002.010", None, None, None, 0),
        (10, 10, 1, "198.051.100.000", "198.051.100.255", "text/plain", "none", 0),
        (11, 13, 1, "login-error.png", base64.b64encode(PNG).decode(), "image/png", "base64", 0),
        (12, 14, 3, "contract.pdf", PDF_SHA, "application/pdf", "external", 0),
        (13, 3, 3, "Mobile", None, None, None, 0),
        (14, 15, 9, "บริษัท ตัวอย่าง จำกัด", None, None, None, 0),
        (15, 2, 1, "Retired value", None, None, None, 1),
    ]
    for value_id, field_id, ticket_id, content, large, content_type, encoding, disabled in values:
        add(
            "ObjectCustomFieldValues",
            id=value_id,
            customfield=field_id,
            objecttype="RT::Ticket",
            objectid=ticket_id,
            content=content,
            largecontent=large,
            contenttype=content_type,
            contentencoding=encoding,
            disabled=disabled,
            sortorder=0,
            **_audit(T3, ALICE),
        )
    cf = {"referencetype": "RT::ObjectCustomFieldValue"}
    txn(1, "CustomField", T3, ALICE, field="2", newreference=1, **cf)
    txn(1, "CustomField", T3, ALICE, field="2", oldreference=1, newreference=2, **cf)
    txn(1, "CustomField", T3, ALICE, field="3", newreference=4, **cf)
    txn(1, "CustomField", T3, ALICE, field="3", oldreference=4, **cf)
    txn(1, "CustomField", T3, ALICE, field="13", newreference=11, **cf)

    # Ticket 2: plain text, time worked by someone else on RT 6.0.
    create2 = txn(2, "Create", T0, BOB)
    part(create2, 0, "text/plain", "Reset mails take an hour.\n", headers=headers("text/plain; charset=utf-8"))
    six = {"timeworker": ALICE, "timeworkeddate": date(2025, 12, 31)} if version == "6.0" else {}
    txn(2, "Comment", T1, BOB, timetaken=45, **six)
    txn(2, "Set", T1, BOB, field="TimeWorked", oldvalue="45", newvalue="65", timetaken=20)
    txn(2, "Set", T2, BOB, field="TimeWorked", oldvalue="65", newvalue="60", timetaken=-5)

    # Ticket 3 refers to ticket 1 and 2 in text; links of every kind.
    create3 = txn(3, "Create", T0, BOB)
    part(create3, 0, "text/plain", "Bill the work on RT #1, see also ticket 2 and ticket 9999.\n")
    txn(3, "AddLink", T1, BOB, field="MemberOf", newvalue="fsck.com-rt://example.com/ticket/1")
    txn(3, "AddLink", T1, BOB, field="DependsOn", newvalue="fsck.com-rt://example.com/ticket/2")

    # Ticket 4 and 5 were merged into 1.
    create4 = txn(4, "Create", T0, ALICE)
    part(create4, 0, "text/plain", "Same as [example.com #1].\n")
    txn(4, "AddLink", T1, ALICE, field="MergedInto", newvalue="fsck.com-rt://example.com/ticket/1")
    create5 = txn(5, "Create", T0, DANA)
    part(create5, 0, "text/plain", "Any news?\n")
    followup = txn(5, "Correspond", T1, DANA)
    mixed5 = part(followup, 0, "multipart/mixed", "")
    part(followup, mixed5, "text/plain", "Another screenshot.\n")
    part(
        followup,
        mixed5,
        "image/png",
        base64.b64encode(PNG).decode(),
        contentencoding="base64",
        filename="second.png",
    )

    for ticket_id in (6, 7, 8, 9, 10, 11, 12, 13):
        created = txn(ticket_id, "Create", T0, ALICE)
        part(created, 0, "text/plain", f"Ticket {ticket_id}.\n" if ticket_id != 9 else "ยอดเงินไม่ถูกต้อง ดู ticket #3\n")

    links = [
        (1, "fsck.com-rt://example.com/ticket/3", "fsck.com-rt://example.com/ticket/1", "MemberOf", 3, 1),
        (2, "fsck.com-rt://example.com/ticket/3", "fsck.com-rt://example.com/ticket/2", "DependsOn", 3, 2),
        (3, "fsck.com-rt://example.com/ticket/3", "fsck.com-rt://example.com/ticket/10", "RefersTo", 3, 10),
        (4, "fsck.com-rt://example.com/ticket/3", "https://www.example.com/spec", "RefersTo", 3, 0),
        (5, "fsck.com-rt://example.com/ticket/5", "fsck.com-rt://example.com/ticket/1", "MergedInto", 5, 1),
        (6, "fsck.com-rt://example.com/ticket/11", "fsck.com-rt://example.com/ticket/1", "RefersTo", 11, 1),
        (7, "fsck.com-rt://example.com/ticket/3", "fsck.com-rt://example.com/ticket/5", "RefersTo", 3, 5),
        (8, "fsck.com-rt://example.com/ticket/7", "fsck.com-rt://example.com/ticket/6", "RefersTo", 7, 6),
    ]
    for link_id, base, target, kind, local_base, local_target in links:
        add(
            "Links",
            id=link_id,
            base=base,
            target=target,
            type=kind,
            localbase=local_base,
            localtarget=local_target,
            creator=BOB,
            created=T1,
        )

    # -- articles -------------------------------------------------------
    add("Classes", id=1, name="General", description="The default class", disabled=0, **_audit())
    add("Classes", id=2, name="Support Knowledge", description="How we help", disabled=0, **_audit())
    add("Classes", id=3, name="FAQ", description="Questions", disabled=0, **_audit())
    add("ObjectClasses", id=1, **{"class": 1}, objecttype="RT::System", objectid=0)
    add("ObjectClasses", id=2, **{"class": 2}, objecttype="RT::Queue", objectid=SUPPORT)
    add("ObjectClasses", id=3, **{"class": 3}, objecttype="RT::System", objectid=0)
    articles = [
        (1, "Resetting a password", "How to reset a password", 2, 0),
        (2, "วิธีติดต่อฝ่ายบริการ", "ติดต่อเรา", 3, 0),
        (3, "Fax numbers", "No longer used", 3, 1),
    ]
    for article_id, name, summary, class_id, disabled in articles:
        add(
            "Articles",
            id=article_id,
            name=name,
            summary=summary,
            **{"class": class_id},
            disabled=disabled,
            sortorder=0,
            parent=0,
            **_audit(T0, ALICE),
        )
    article_values = [
        (101, 1, "<p>Open the <b>login page</b>.</p>", 1),
        (102, 1, "<p>Open the <b>login page</b> and check your mail.</p>", 0),
        (103, 2, "<p>โทร 02 000 0000</p>", 0),
        (104, 3, "<p>No fax.</p>", 0),
    ]
    for value_id, article_id, content, disabled in article_values:
        add(
            "ObjectCustomFieldValues",
            id=value_id,
            customfield=1,
            objecttype="RT::Article",
            objectid=article_id,
            content=content,
            disabled=disabled,
            sortorder=0,
            **_audit(T0, ALICE),
        )
    txn(1, "Create", T0, ALICE, objecttype="RT::Article")
    txn(1, "CustomField", T2, BOB, objecttype="RT::Article", field="1", oldreference=101, newreference=102, **cf)
    txn(2, "Create", T0, ALICE, objecttype="RT::Article")

    # -- counted only ---------------------------------------------------
    add("Assets", id=1)
    add("Catalogs", id=1)
    add("Scrips", id=1, disabled=0)
    add("Templates", id=1)
    add("Attributes", id=1, name="SavedSearch", objecttype="RT::User", objectid=BOB, content="{}")
    add("Topics", id=1, parent=0, name="Accounts")
    if version == "6.0":
        add("SavedSearches", id=1, disabled=0)
        add("Dashboards", id=1, disabled=0)
    return rows
