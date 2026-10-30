"""The whole export, over the sample RT: what each record of the bundle holds."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from specivo_migration_sdk import validate_bundle

from specivo_rt_migration.cli import main
from specivo_rt_migration.export import PRIVILEGED_GROUP_REF
from tests import sample_rt
from tests.conftest import Bundle, Export, read_bundle
from tests.sample_rt import (
    ALICE,
    BILLING,
    BOB,
    CAROL,
    DANA,
    EVERYONE,
    GENERAL,
    OLD,
    PRIVILEGED,
    ROOT,
    SOMCHAI,
    SUPPORT,
    T0,
    T1,
    T3,
    THAI,
)


def _txn(txn_id: int, ticket: int, kind: str, when: datetime, creator: int, **values: Any) -> dict[str, Any]:
    """A row of ``Transactions`` on a ticket, for a test's own case."""
    row = {"id": txn_id, "objecttype": "RT::Ticket", "objectid": ticket, "type": kind, "timetaken": 0}
    return {**row, "created": when, "creator": creator, **values}


def _part(part_id: int, txn: int, parent: int, content_type: str, content: Any, **values: Any) -> dict[str, Any]:
    """A row of ``Attachments``: one MIME part of a transaction's message."""
    row = {"id": part_id, "transactionid": txn, "parent": parent, "contenttype": content_type, "content": content}
    return {"headers": f"Content-Type: {content_type}\n", "creator": 1, "created": T3, **row, **values}


@pytest.fixture
def bundle(export: Export) -> Bundle:
    return export()


@pytest.mark.parametrize("version", ["4.4", "5.0", "6.0"])
def test_every_version_gives_a_valid_bundle(version: str, tmp_path: Path, export: Export) -> None:
    source = sample_rt.build(tmp_path / f"rt-{version}", version=version)
    bundle = export(source=source)
    report = validate_bundle(bundle.root)
    assert report.ok, report.format_text()
    assert bundle.manifest["source"] == {"system": "rt", "instance": "rt.example.com", "version": version}


def test_queues_become_projects(bundle: Bundle) -> None:
    projects = bundle.by_ref("projects")
    assert projects["3"]["identifier"] == "support"
    assert projects["3"]["name"] == "Support"
    assert projects["5"]["status"] == 9  # disabled: archived
    assert projects["6"]["identifier"] == "queue-6"  # a name in Thai
    assert projects["6"]["name"] == "งานภาษาไทย"
    # The approvals queue is RT's own; its tickets are approvals, which are counted.
    assert "2" not in projects
    assert bundle.not_carried()["approval tickets"] == 1


def test_project_identifiers_are_unique(sample: sample_rt.SampleRT, export: Export) -> None:
    """Two queues give the same identifier, and a queue the one of the FAQ article class."""
    sample_rt.insert(
        sample,
        "Queues",
        *(
            {"id": queue_id, "name": name, "lifecycle": "default", "disabled": 0, "sladisabled": 1}
            for queue_id, name in ((7, "IT Help"), (8, "IT-Help"), (9, "KB FAQ"))
        ),
    )
    projects = export().by_ref("projects")
    identifiers = {ref: project["identifier"] for ref, project in projects.items()}
    assert (identifiers["7"], identifiers["8"]) == ("it-help", "it-help-2")
    assert (identifiers["9"], identifiers["class:3"]) == ("kb-faq", "kb-faq-2")
    assert len(set(identifiers.values())) == len(identifiers)


def test_tickets_become_issues_in_id_order(bundle: Bundle) -> None:
    issues = [issue["source_ref"] for issue in bundle.get("issues")]
    # 4 and 5 were merged into 1, 6 is deleted, 11 a reminder, 12 an approval.
    assert issues == ["1", "2", "7", "13", "3", "10", "8", "9"]
    assert bundle.not_carried()["deleted tickets"] == 1


def test_issue_fields(bundle: Bundle) -> None:
    issue = bundle.by_ref("issues")["1"]
    assert issue["project_ref"] == "3"
    assert issue["status_ref"] == "stalled"
    assert issue["priority_ref"] == "medium"
    assert issue["author_ref"] == str(DANA)
    assert issue["assigned_to_ref"] == str(BOB)
    assert issue["due_date"] == "2026-01-20"
    assert issue["start_date"] == "2026-01-05"
    assert issue["estimated_hours"] == "3.00"
    assert issue["remaining_estimate"] == "1.50"
    assert issue["created_at"] == "2026-01-05T09:00:00Z"
    assert issue["updated_at"] == "2026-01-08T16:45:00Z"
    assert "closed_at" not in issue
    extra = issue["extra"]
    assert extra["rt_ticket"] == 1
    assert extra["priority"] == 80
    assert extra["initial_priority"] == 20
    assert extra["final_priority"] == 90
    assert extra["told"] == "2026-01-06T10:30:00Z"
    assert "started" not in extra  # 1970-01-01 means never
    assert extra["merged_tickets"] == [4, 5]
    assert extra["requestors"] == ["dana@example.net"]
    assert extra["roles"]["AdminCc"] == ["somchai"]
    assert extra["reminders"] == [
        {
            "rt_ticket": 11,
            "subject": "Call the customer back",
            "status": "open",
            "owner": "bob",
            "due": "2026-01-08T16:45:00Z",
        }
    ]


def test_unowned_ticket_has_no_assignee_and_closed_ticket_a_close_time(bundle: Bundle) -> None:
    issues = bundle.by_ref("issues")
    assert "assigned_to_ref" not in issues["13"]
    assert issues["7"]["closed_at"] == "2026-01-07T14:15:00Z"
    assert issues["7"]["status_ref"] == "rejected"


def test_description_is_the_first_message(export: Export, tmp_path: Path) -> None:
    bundle = export(source=sample_rt.build(tmp_path / "rt-5", version="5.0"))
    description = bundle.by_ref("issues")["1"]["description"]
    assert description.startswith("The **login page** shows an error. See {{issue:1}}.")
    assert "![the error]({{attachment:" in description
    assert "alert" not in description
    assert "ภาษาไทย: เข้าสู่ระบบไม่ได้" in description


def test_rt6_description_and_first_message(bundle: Bundle) -> None:
    """RT 6.0 has a description of its own; the first message then becomes the first journal."""
    issue = bundle.by_ref("issues")["1"]
    assert issue["description"] == "An **own** description."
    first = bundle.by_ref("journals")["1"]
    assert first["notes"].startswith("The **login page** shows an error.")
    assert first["extra"]["message_id"] == "first-request@example.net"
    assert first["extra"]["subject"] == "Login page is broken"
    assert first["extra"]["cc"] == ["eve@example.org"]


def test_replies_and_comments(bundle: Bundle) -> None:
    journals = [journal for journal in bundle.get("journals") if journal["issue_ref"] == "1"]
    reply = next(journal for journal in journals if journal.get("notes", "").startswith("We are"))
    assert reply["notes"] == "We are *on it*. {{issue:2}} is related."
    assert reply.get("is_private", False) is False
    assert reply["user_ref"] == str(BOB)
    # Owner, status and reply in one second by one person: one journal.
    keys = [detail["prop_key"] for detail in reply["details"]]
    assert keys == ["assigned_to_id", "status_id"]
    # Nobody is no one: the assignee was unset.
    assert reply["details"][0] == {
        "property": "attr",
        "prop_key": "assigned_to_id",
        "new_value": "bob",
        "new_ref": str(BOB),
    }

    comment = next(journal for journal in journals if journal.get("is_private"))
    assert comment["user_ref"] == str(ALICE)
    assert comment["notes"] == "Internal: cookie \\*expires\\* early.\\\n\\# not a heading\\\n{{issue:3}} too."
    # The comment's file is left out: an attachment of the issue would not be private.
    assert "details" not in comment

    thai = next(journal for journal in journals if journal.get("notes") == "ขอบคุณครับ")
    assert thai["user_ref"] == str(DANA)


def test_field_changes(bundle: Bundle) -> None:
    journals = [journal for journal in bundle.get("journals") if journal["issue_ref"] == "1"]
    details = [detail for journal in journals for detail in journal.get("details", [])]
    by_key: dict[str, list[dict[str, object]]] = {}
    for detail in details:
        by_key.setdefault(str(detail["prop_key"]), []).append(detail)
    assert by_key["subject"][0]["new_value"] == "Login page is broken!"
    priority = by_key["priority_id"][0]
    assert (priority["old_ref"], priority["new_ref"]) == ("low", "medium")
    assert by_key["due_date"][0]["new_value"] == "2026-01-20"
    assert by_key["due_date"][0].get("old_value") is None
    queues = by_key["project_id"]
    assert [(change["old_ref"], change["new_ref"]) for change in queues] == [("3", "4"), ("4", "3")]
    assert queues[0]["new_value"] == "Billing"
    assert by_key["admincc"][0]["new_value"] == "somchai"
    assert by_key["cc"][0]["old_value"] == "dana@example.net"
    status = [change for change in by_key["status_id"] if change.get("new_ref") == "stalled"][0]
    assert status["old_ref"] == "open"


def test_custom_field_history(bundle: Bundle) -> None:
    details = [
        detail
        for journal in bundle.get("journals")
        if journal["issue_ref"] == "1"
        for detail in journal.get("details", [])
        if detail["property"] == "cf"
    ]
    severity = [(d.get("old_value"), d.get("new_value")) for d in details if d["prop_key"] == "2"]
    assert severity == [(None, "High"), ("High", "Critical")]
    components = [(d.get("old_value"), d.get("new_value")) for d in details if d["prop_key"] == "3"]
    assert components == [(None, "API"), ("API", None)]
    # The Image field's history is not carried: its values are files.
    assert not [d for d in details if d["prop_key"] == "13"]


def test_custom_values(bundle: Bundle) -> None:
    values = {value["key"]: value["value"] for value in bundle.by_ref("issues")["1"]["custom_values"]}
    assert values == {
        "severity": "Critical",
        "components": ["Web"],
        "customer_reference": "cust0042",
        "notes": "First line\nSecond line",
        "go_live_date": "2026-12-01",
        "maintenance_window": "2026-12-01T15:00:00Z",
        "server_address": "192.0.2.10",
        "network_range": "198.51.100.0-198.51.100.255",
    }
    thai = bundle.by_ref("issues")["9"]["custom_values"]
    assert thai == [{"field_ref": "15", "key": "cf_15", "value": "บริษัท ตัวอย่าง จำกัด"}]


def test_custom_field_definitions(bundle: Bundle) -> None:
    fields = bundle.by_ref("custom_fields")
    assert fields["2"]["field_format"] == "list"
    assert fields["2"]["json_schema"] == {"type": "string", "enum": ["Low", "High", "Critical"]}
    assert fields["3"]["multiple"] is True
    assert fields["3"]["json_schema"]["type"] == "array"
    assert fields["3"]["project_refs"] == ["3", "4"]
    assert fields["4"]["is_for_all"] is True
    assert fields["7"]["field_format"] == "date"
    assert fields["8"]["json_schema"] == {"type": "string", "format": "date-time"}
    assert fields["5"]["field_format"] == "text"
    # Image and Binary fields have no field: their values are files.
    assert "13" not in fields and "14" not in fields
    assert fields["15"]["name"] == "ลูกค้า"
    assert bundle.not_carried()["custom fields of queues"] == 1


def test_files(bundle: Bundle) -> None:
    attachments = {a["filename"]: a for a in bundle.get("attachments")}
    assert set(attachments) == {
        "error.png",
        "invoice.pdf",
        "second.png",
        "login-error.png",
        "contract.pdf",
    }
    pdf = attachments["invoice.pdf"]
    assert pdf["sha256"] == sample_rt.PDF_SHA
    assert pdf["container_ref"] == "1"
    assert pdf["author_ref"] == str(DANA)
    assert attachments["error.png"]["extra"]["content_id"] == "shot@example.net"
    assert attachments["login-error.png"]["source_ref"].startswith("cf-value:")
    assert attachments["contract.pdf"]["container_ref"] == "3"
    # The merged ticket's file is on the surviving issue.
    assert attachments["second.png"]["container_ref"] == "1"
    assert (bundle.root / "files" / sample_rt.PDF_SHA).read_bytes() == sample_rt.PDF


def test_files_of_comments_are_left_out_unless_asked(bundle: Bundle, export: Export) -> None:
    assert "server.log" not in {a["filename"] for a in bundle.get("attachments")}
    assert bundle.not_carried()["files of private comments (--include-comment-files carries them)"] == 1

    carried = export("--include-comment-files")
    attachment = next(a for a in carried.get("attachments") if a["filename"] == "server.log")
    assert attachment["author_ref"] == str(ALICE)
    comment = next(j for j in carried.get("journals") if j["issue_ref"] == "1" and j.get("is_private"))
    assert comment["details"] == [
        {"property": "attachment", "prop_key": attachment["source_ref"], "new_value": "server.log"}
    ]
    assert "files of private comments (--include-comment-files carries them)" not in carried.not_carried()


def _comment_with_inline_image(sample: sample_rt.SampleRT) -> None:
    """Ticket 2: Alice comments in HTML, showing a screenshot by its Content-ID."""
    sample_rt.insert(sample, "Transactions", _txn(1001, 2, "Comment", T3, ALICE))
    sample_rt.insert(
        sample,
        "Attachments",
        _part(1001, 1001, 0, "multipart/related", ""),
        _part(1002, 1001, 1001, "text/html", '<p>Look:</p><p><img src="cid:note@example.com" alt="shot"></p>'),
        _part(
            1003,
            1001,
            1001,
            "image/png",
            base64.b64encode(sample_rt.PNG).decode(),
            contentencoding="base64",
            filename="note.png",
            headers="Content-Type: image/png\nContent-ID: <note@example.com>\nContent-Disposition: inline\n",
        ),
    )


def test_inline_image_of_a_private_note_is_left_out(sample: sample_rt.SampleRT, export: Export) -> None:
    _comment_with_inline_image(sample)
    bundle = export()
    note = next(j for j in bundle.get("journals") if j["source_ref"] == "1001")
    assert note["is_private"] is True
    assert note["notes"] == "Look:"
    assert "note.png" not in {a["filename"] for a in bundle.get("attachments")}

    carried = export("--include-comment-files")
    note = next(j for j in carried.get("journals") if j["source_ref"] == "1001")
    assert note["notes"] == "Look:\n\n![shot]({{attachment:1003}})"


def test_a_comment_is_private_by_its_type_and_never_shares_an_entry(sample: sample_rt.SampleRT, export: Export) -> None:
    """One second, one person: a status change, a comment with only a file, a reply. Three entries."""
    sample_rt.insert(
        sample,
        "Transactions",
        _txn(1001, 2, "Status", T3, BOB, field="Status", oldvalue="new", newvalue="open"),
        _txn(1002, 2, "Comment", T3, BOB),
        _txn(1003, 2, "Correspond", T3, BOB),
    )
    sample_rt.insert(
        sample,
        "Attachments",
        _part(1001, 1002, 0, "text/plain", sample_rt.LOG.decode(), filename="debug.log"),
        _part(1002, 1003, 0, "text/plain", "Fixed now.\n"),
    )
    bundle = export("--include-comment-files")
    journals = {j["source_ref"]: j for j in bundle.get("journals") if j["source_ref"] in ("1001", "1002", "1003")}
    assert set(journals) == {"1001", "1002", "1003"}
    assert journals["1001"].get("is_private", False) is False
    assert [d["prop_key"] for d in journals["1001"]["details"]] == ["status_id"]
    assert journals["1002"]["is_private"] is True
    assert "notes" not in journals["1002"]
    assert journals["1002"]["details"][0]["new_value"] == "debug.log"
    assert journals["1003"].get("is_private", False) is False
    assert journals["1003"]["notes"] == "Fixed now."


def test_files_in_external_storage_without_the_path_are_counted(export: Export) -> None:
    bundle = export(storage=False)
    names = {a["filename"] for a in bundle.get("attachments")}
    assert "invoice.pdf" not in names and "contract.pdf" not in names
    not_carried = bundle.not_carried()
    assert not_carried["files in RT's external storage, not read (give --external-storage-path for Disk)"] == 1
    assert not_carried["custom field files in RT's external storage, not read"] == 1
    assert validate_bundle(bundle.root).ok


def test_merged_tickets_fold_into_the_survivor(bundle: Bundle) -> None:
    journals = [journal for journal in bundle.get("journals") if journal["issue_ref"] == "1"]
    merged = [journal for journal in journals if journal.get("extra", {}).get("merged_from")]
    assert {journal["extra"]["merged_from"] for journal in merged} == {4, 5}
    created = next(journal for journal in merged if journal["extra"].get("merged_ticket_created") == 4)
    # A reference to the merged ticket's own number resolves to the survivor.
    assert created["notes"] == "Same as {{issue:1}}."
    assert bundle.not_carried()["merged tickets (folded into the ticket they were merged into)"] == 2


def _created(sample: sample_rt.SampleRT, ticket: int, txn: int, text: str, creator: int = DANA, **values: Any) -> None:
    """A Create transaction of *ticket* with a plain text message (part id = transaction id)."""
    sample_rt.insert(sample, "Transactions", _txn(txn, ticket, "Create", T0, creator, **values))
    sample_rt.insert(sample, "Attachments", _part(txn, txn, 0, "text/plain", text))


def test_merged_ticket_of_a_queue_not_exported_is_left_out(sample: sample_rt.SampleRT, export: Export) -> None:
    """Ticket 20 of Billing was merged into ticket 1 of Support: Billing's text stays out of Support."""
    sample_rt.insert(
        sample,
        "Tickets",
        sample_rt.ticket_row(20, BILLING, "Billing question", effectiveid=1),
        sample_rt.ticket_row(21, SUPPORT, "Spam, merged", effectiveid=1, status="deleted"),
    )
    _created(sample, 20, 1001, "Billing only.\n")
    _created(sample, 21, 1002, "Spam.\n")

    support = export("--queue", "Support")
    issue = support.by_ref("issues")["1"]
    assert issue["extra"]["merged_tickets"] == [4, 5]
    assert {j.get("extra", {}).get("merged_from") for j in support.get("journals")} == {None, 4, 5}
    not_carried = support.not_carried()
    assert not_carried["merged tickets of queues not exported (left out of the ticket they were merged into)"] == 1
    assert not_carried["merged tickets that are deleted, reminders or approvals"] == 1
    assert not_carried["merged tickets (folded into the ticket they were merged into)"] == 2

    everything = export()
    assert everything.by_ref("issues")["1"]["extra"]["merged_tickets"] == [4, 5, 20]
    folded = everything.by_ref("journals")["1001"]
    assert (folded["issue_ref"], folded["notes"], folded["extra"]["merged_from"]) == ("1", "Billing only.", 20)


def test_merged_tickets_whose_survivor_is_not_in_the_bundle_are_counted(
    sample: sample_rt.SampleRT, export: Export
) -> None:
    """Merged into a deleted ticket (6), one cut by --limit (13), one of another queue (3)."""
    sample_rt.insert(
        sample,
        "Tickets",
        sample_rt.ticket_row(21, SUPPORT, "Into the deleted one", effectiveid=6),
        sample_rt.ticket_row(22, SUPPORT, "Into the weekly report", effectiveid=13),
        sample_rt.ticket_row(23, SUPPORT, "Into the invoice", effectiveid=3),
    )
    sample_rt.insert(sample, "Transactions", _txn(1001, 2, "Correspond", T3, BOB))
    sample_rt.insert(
        sample, "Attachments", _part(1001, 1001, 0, "text/plain", "See ticket 21, ticket 22 and ticket 23.\n")
    )

    bundle = export("--queue", "Support", "--limit", "2")
    key = (
        "merged tickets whose surviving ticket is not in this bundle "
        "(another queue, deleted, a reminder or approval, or cut by --limit)"
    )
    assert bundle.not_carried()[key] == 3
    assert bundle.not_carried()["merged tickets (folded into the ticket they were merged into)"] == 2  # 4 and 5
    # Each number leads where RT leads it: nowhere for a deleted ticket, else to the surviving ticket's bundle.
    reply = bundle.by_ref("journals")["1001"]
    assert reply["notes"] == "See ticket 21, {{issue:13|ticket 22}} and {{issue:3|ticket 23}}."


def test_field_changes_of_a_merged_ticket_are_not_replayed(sample: sample_rt.SampleRT, export: Export) -> None:
    """Ticket 5 (merged into 1) had changes of its own; only its messages and time are folded."""
    sample_rt.insert(
        sample,
        "Transactions",
        _txn(1001, 5, "Status", T3, DANA, field="Status", oldvalue="new", newvalue="resolved"),
        _txn(1002, 5, "Set", T3, DANA, field="Subject", oldvalue="Still cannot log in", newvalue="Fine now"),
        _txn(1003, 5, "Give", T3, ALICE, field="Owner", oldvalue=str(sample_rt.NOBODY), newvalue=str(ALICE)),
        _txn(1004, 5, "Comment", T3, ALICE, timetaken=10),
    )
    sample_rt.insert(sample, "Attachments", _part(1004, 1004, 0, "text/plain", "Checked the logs.\n"))
    bundle = export()
    journals = bundle.by_ref("journals")
    assert not {"1001", "1002", "1003"} & set(journals)
    assert journals["1004"]["issue_ref"] == "1"
    assert journals["1004"]["extra"]["merged_from"] == 5
    assert journals["1004"]["is_private"] is True
    entry = bundle.by_ref("time_entries")["1004"]
    assert (entry["issue_ref"], entry["hours"], entry["extra"]["merged_from"]) == ("1", "0.17", 5)
    key = "field changes of merged tickets (not replayed on the ticket they were merged into)"
    assert bundle.not_carried()[key] == 3


def test_time_worked_at_creation(sample: sample_rt.SampleRT, export: Export) -> None:
    """RT keeps the time worked given on creation as the Create transaction's TimeTaken."""
    sample_rt.insert(
        sample,
        "Tickets",
        sample_rt.ticket_row(25, SUPPORT, "Set up the printer", timeworked=45),
        sample_rt.ticket_row(26, SUPPORT, "Printer again", effectiveid=25, timeworked=15),
    )
    _created(sample, 25, 1001, "Done on the phone.\n", creator=BOB, timetaken=30)
    _created(sample, 26, 1002, "Same printer.\n", creator=ALICE, timetaken=15)
    entries = export().by_ref("time_entries")
    assert (entries["1001"]["issue_ref"], entries["1001"]["hours"], entries["1001"]["user_ref"]) == (
        "25",
        "0.50",
        str(BOB),
    )
    assert (entries["1002"]["issue_ref"], entries["1002"]["hours"]) == ("25", "0.25")
    assert entries["1002"]["extra"]["merged_from"] == 26


def test_links(bundle: Bundle) -> None:
    issues = bundle.by_ref("issues")
    assert issues["3"]["parent_ref"] == "1"
    relations = {(r["from_ref"], r["to_ref"], r["relation_type"]) for r in bundle.get("relations")}
    # DependsOn: 3 depends on 2, so 2 blocks 3. RefersTo: relates, once per pair.
    # RefersTo 5 (merged into 1) would relate 3 to its own parent, which Specivo refuses.
    assert relations == {("2", "3", "blocks"), ("3", "10", "relates")}
    assert issues["3"]["extra"]["links"] == [{"type": "RefersTo", "uri": "https://www.example.com/spec"}]
    not_carried = bundle.not_carried()
    assert not_carried["links to tickets that are deleted, reminders, approvals or no longer exist"] == 1
    assert not_carried["relations between a ticket and its parent or child (the parent is kept)"] == 1


def _links_to_other_rt(sample: sample_rt.SampleRT) -> None:
    """Ticket 2 refers to ticket 3 of another RT, and to ticket 10 by a URI RT did not store as local."""
    link = {"type": "RefersTo", "localbase": 2, "localtarget": 0, "creator": BOB, "created": T1}
    sample_rt.insert(
        sample,
        "Links",
        {"id": 20, "base": "fsck.com-rt://example.com/ticket/2", "target": OTHER_RT_TICKET, **link},
        {
            "id": 21,
            "base": "fsck.com-rt://example.com/ticket/2",
            "target": "fsck.com-rt://example.com/ticket/10",
            **link,
        },
    )
    sample_rt.insert(
        sample, "Transactions", _txn(1001, 2, "AddLink", T1, BOB, field="RefersTo", newvalue=OTHER_RT_TICKET)
    )


OTHER_RT_TICKET = "fsck.com-rt://rt.example.org/ticket/3"


def test_links_to_tickets_of_another_rt_are_kept_as_uris(sample: sample_rt.SampleRT, export: Export) -> None:
    """RT's organization is the host of the URIs it stored as local: example.com."""
    _links_to_other_rt(sample)
    bundle = export()
    relations = {(r["from_ref"], r["to_ref"], r["relation_type"]) for r in bundle.get("relations")}
    assert ("2", "3", "relates") not in relations
    assert ("2", "10", "relates") in relations
    assert bundle.by_ref("issues")["2"]["extra"]["links"] == [{"type": "RefersTo", "uri": OTHER_RT_TICKET}]
    assert bundle.not_carried()["links to tickets of other RT servers (kept in the issue's extra)"] == 1
    detail = bundle.by_ref("journals")["1001"]["details"][0]
    assert (detail["prop_key"], detail["new_value"]) == ("relates", OTHER_RT_TICKET)


def test_organization_from_rt_settings(sample: sample_rt.SampleRT, export: Export) -> None:
    """With $Organization set to rt.example.org, its URIs are local and example.com's are not."""
    _links_to_other_rt(sample)
    sample_rt.insert(
        sample, "Configurations", {"id": 10, "name": "Organization", "contenttype": "perl", "disabled": 0,
                                   "content": "'rt.example.org'"}
    )  # fmt: skip
    bundle = export()
    relations = {(r["from_ref"], r["to_ref"], r["relation_type"]) for r in bundle.get("relations")}
    # Link 20 names rt.example.org's ticket 3, which is this RT's ticket 3 now.
    assert ("2", "3", "relates") in relations
    assert ("2", "10", "relates") not in relations
    links = bundle.by_ref("issues")["2"]["extra"]["links"]
    assert links == [{"type": "RefersTo", "uri": "fsck.com-rt://example.com/ticket/10"}]
    assert bundle.by_ref("journals")["1001"]["details"][0]["new_value"] == "#3"


def test_without_the_organization_only_links_rt_stored_as_local_count(
    sample: sample_rt.SampleRT, export: Export
) -> None:
    """No setting and no link RT stored as local: a ticket URI in history stays a URI."""
    for link_id in range(1, 9):
        sample_rt.update(sample, "Links", link_id, localbase=0, localtarget=0)
    bundle = export()
    assert "parent_ref" not in bundle.by_ref("issues")["3"]
    member_of = [
        detail
        for journal in bundle.get("journals")
        if journal["issue_ref"] == "3"
        for detail in journal.get("details", [])
        if detail["prop_key"] == "parent_id"
    ]
    assert member_of == [
        {"property": "attr", "prop_key": "parent_id", "new_value": "fsck.com-rt://example.com/ticket/1"}
    ]


def test_text_references(bundle: Bundle) -> None:
    description = bundle.by_ref("issues")["3"]["description"]
    # 9999 is no ticket of RT: left as it was.
    assert description == "Bill the work on {{issue:1}}, see also {{issue:2}} and ticket 9999."


def test_reference_to_a_ticket_outside_the_bundle_keeps_its_text(export: Export) -> None:
    bundle = export("--queue", "Billing")
    description = bundle.by_ref("issues")["3"]["description"]
    assert description == "Bill the work on {{issue:1|RT #1}}, see also {{issue:2|ticket 2}} and ticket 9999."


def test_time_entries(bundle: Bundle) -> None:
    entries = bundle.by_ref("time_entries")
    assert {ref: entry["hours"] for ref, entry in entries.items()} == {
        ref: hours for ref, hours in zip(entries, ["0.25", "0.50", "0.75", "0.33"], strict=True)
    }
    worked = [entry for entry in entries.values() if entry["issue_ref"] == "2"]
    # RT 6.0 says who did the work and when.
    assert worked[0]["user_ref"] == str(ALICE)
    assert worked[0]["spent_on"] == "2025-12-31"
    assert worked[1]["user_ref"] == str(BOB)
    assert all(entry["activity_ref"] == "work" and entry["project_ref"] == "3" for entry in entries.values())
    assert bundle.not_carried()["negative time corrections (time worked reduced in RT)"] == 1


def test_time_entries_before_rt6_are_the_recorders(export: Export, tmp_path: Path) -> None:
    bundle = export(source=sample_rt.build(tmp_path / "rt-4", version="4.4"))
    worked = [entry for entry in bundle.get("time_entries") if entry["issue_ref"] == "2"]
    assert worked[0]["user_ref"] == str(BOB)
    assert worked[0]["spent_on"] == "2026-01-06"


def test_watchers(bundle: Bundle) -> None:
    watchers = {(w["container_ref"], w["user_ref"]) for w in bundle.get("watchers")}
    # Dana is the requestor; Somchai the AdminCc, and a Cc through the Thai Desk group.
    assert watchers == {("1", str(DANA)), ("1", str(SOMCHAI))}


def test_users(bundle: Bundle) -> None:
    users = bundle.by_ref("users")
    assert set(users) == {"14", "20", "21", "22", "23", "24"}  # not RT_System, not Nobody
    assert users["20"]["login"] == "alice"
    assert users["20"].get("status", "active") == "active"
    assert users["23"]["status"] == "locked"  # disabled
    assert users["24"]["status"] == "locked"  # unprivileged
    assert users["24"]["extra"]["privileged"] is False
    assert users["14"]["is_admin"] is True
    assert users["22"]["display_name"] == "สมชาย ใจดี"
    assert users["22"]["language"] == "th"


def test_a_partial_export_carries_the_people_its_queues_name(export: Export) -> None:
    billing = export("--queue", "Billing")
    # Bob created both tickets and manages Billing, Alice wrote a first message; both are in the
    # Support Team, whose membership in Billing comes along. Nobody else is named there.
    assert set(billing.by_ref("users")) == {str(ALICE), str(BOB)}
    assert set(billing.by_ref("groups")) == {"40"}
    assert {m["principal_ref"] for m in billing.get("memberships")} == {"40", str(BOB)}

    support = export("--queue", "Support")
    # Dana requested, Somchai watches (directly and through the Thai Desk, which is no member of Support).
    assert set(support.by_ref("users")) == {str(ALICE), str(BOB), str(SOMCHAI), str(DANA)}
    assert set(support.by_ref("groups")) == {"40"}
    assert validate_bundle(support.root).ok


def test_unprivileged_users_can_be_made_active(export: Export) -> None:
    users = export("--activate-unprivileged").by_ref("users")
    assert users["24"].get("status", "active") == "active"
    assert users["23"]["status"] == "locked"


def test_groups_hold_users_through_nested_groups(bundle: Bundle) -> None:
    groups = bundle.by_ref("groups")
    assert groups["40"]["member_refs"] == ["20", "21"]
    assert groups["42"]["name"] == "All Agents"
    assert groups["42"]["member_refs"] == ["20", "21", "22"]


def test_memberships_from_rights(bundle: Bundle) -> None:
    memberships = {
        (m["project_ref"], m["principal_kind"], m["principal_ref"]): m["role_refs"] for m in bundle.get("memberships")
    }
    assert memberships == {
        ("3", "group", "40"): ["developer"],  # Support Team: OwnTicket on Support
        ("4", "group", "40"): ["developer"],  # ModifyTicket on Billing
        ("4", "user", "21"): ["manager"],  # Bob: AdminQueue on Billing
        ("6", "group", "41"): ["reporter"],  # Thai Desk: ShowTicket only
        ("3", "user", "22"): ["developer"],  # Somchai, AdminCc of Support, whose role may modify
    }
    not_carried = bundle.not_carried()
    assert not_carried["rights granted to everyone in RT's Everyone group"] == 1
    assert not_carried["rights granted to the Requestor role of each ticket"] == 1
    # Privileged holds no right on a queue in the sample: no group stands for it.
    assert PRIVILEGED_GROUP_REF not in bundle.by_ref("groups")


def _acl(*rows: tuple[int, str, int]) -> list[dict[str, Any]]:
    """Rows of ``ACL``, each (principal group, right, queue id or 0 for every queue), from id 100 on."""
    return [
        {
            "id": 100 + index,
            "principaltype": "Group",
            "principalid": principal,
            "rightname": right,
            "objecttype": "RT::Queue" if queue else "RT::System",
            "objectid": queue or 1,
        }
        for index, (principal, right, queue) in enumerate(rows)
    ]


def test_rights_of_rt_privileged_group_become_one_group(sample: sample_rt.SampleRT, export: Export) -> None:
    sample_rt.insert(
        sample,
        "ACL",
        *_acl(
            (PRIVILEGED, "ShowTicket", 0),  # every queue
            (PRIVILEGED, "OwnTicket", BILLING),
            (PRIVILEGED, "AdminQueue", THAI),
            (PRIVILEGED, "SeeQueue", GENERAL),  # amounts to no role: counted
        ),
    )
    bundle = export()
    group = bundle.by_ref("groups")[PRIVILEGED_GROUP_REF]
    assert group["name"] == "RT privileged users"
    # Every privileged user, the disabled one too; not Dana, who is unprivileged.
    assert group["member_refs"] == [str(user) for user in (ROOT, ALICE, BOB, SOMCHAI, CAROL)]
    roles = {(m["project_ref"], m["principal_ref"]): m["role_refs"] for m in bundle.get("memberships")}
    privileged = {
        m["project_ref"]: m["role_refs"]
        for m in bundle.get("memberships")
        if m["principal_ref"] == PRIVILEGED_GROUP_REF
    }
    assert privileged == {
        str(GENERAL): ["reporter"],  # ShowTicket only
        str(SUPPORT): ["reporter"],
        str(BILLING): ["developer"],
        str(OLD): ["reporter"],
        str(THAI): ["manager"],
    }
    assert roles[(str(SUPPORT), "40")] == ["developer"]  # the Support Team's membership is still there
    assert bundle.not_carried()["rights granted to everyone in RT's Privileged group"] == 1
    assert validate_bundle(bundle.root).ok


def test_rt_privileged_group_of_a_partial_export_holds_the_users_carried(
    sample: sample_rt.SampleRT, export: Export
) -> None:
    sample_rt.insert(sample, "ACL", *_acl((PRIVILEGED, "ShowTicket", THAI), (PRIVILEGED, "ModifyTicket", BILLING)))
    billing = export("--queue", "Billing")
    assert billing.by_ref("groups")[PRIVILEGED_GROUP_REF]["member_refs"] == [str(ALICE), str(BOB)]
    privileged = [m for m in billing.get("memberships") if m["principal_ref"] == PRIVILEGED_GROUP_REF]
    assert [(m["project_ref"], m["role_refs"]) for m in privileged] == [(str(BILLING), ["developer"])]
    assert "rights granted to everyone in RT's Privileged group" not in billing.not_carried()

    # A queue Privileged holds no role on gets no group.
    support = export("--queue", "Support")
    assert PRIVILEGED_GROUP_REF not in support.by_ref("groups")
    assert all(m["principal_ref"] != PRIVILEGED_GROUP_REF for m in support.get("memberships"))


def test_rights_of_rt_everyone_group_are_reported_not_carried(sample: sample_rt.SampleRT, export: Export) -> None:
    # The sample grants Everyone CreateTicket on every queue.
    sample_rt.insert(sample, "ACL", *_acl((EVERYONE, "ShowTicket", SUPPORT), (EVERYONE, "ReplyToTicket", SUPPORT)))
    bundle = export()
    assert all(m["principal_ref"] != str(EVERYONE) for m in bundle.get("memberships"))
    assert bundle.not_carried()["rights granted to everyone in RT's Everyone group"] == 3
    items = {item["what"]: item for item in bundle.manifest["not_carried_over"]}
    assert "rights granted to RT's Everyone group on every queue: CreateTicket" in items
    support = items["rights granted to RT's Everyone group on queue Support: ReplyToTicket, ShowTicket"]
    assert (support["entity_type"], support["source_ref"]) == ("project", str(SUPPORT))
    projects = bundle.by_ref("projects")
    assert projects[str(SUPPORT)]["extra"]["rt_everyone_rights"] == ["CreateTicket", "ReplyToTicket", "ShowTicket"]
    assert projects[str(BILLING)]["extra"]["rt_everyone_rights"] == ["CreateTicket"]
    assert validate_bundle(bundle.root).ok

    billing = export("--queue", "Billing")
    whats = [item["what"] for item in billing.manifest["not_carried_over"]]
    assert "rights granted to RT's Everyone group on every queue: CreateTicket" in whats
    assert not any("on queue Support" in what for what in whats)


def test_lookups(bundle: Bundle) -> None:
    statuses = {status["source_ref"]: status["category"] for status in bundle.lookups["statuses"]}
    assert statuses["new"] == "backlog"
    assert statuses["stalled"] == "active"
    assert statuses["rejected"] == "closed"
    assert statuses["quoted"] == "backlog"
    assert statuses["paid"] == "closed"
    assert statuses["custom-status"] == "active"  # guessed
    assert "deleted" not in statuses
    priorities = [priority["name"] for priority in bundle.lookups["priorities"]]
    assert priorities == ["Low", "Routine", "Medium", "Urgent", "High"]
    assert [role["name"] for role in bundle.lookups["roles"]] == ["Manager", "Developer", "Reporter"]


def test_status_category_map_overrides(export: Export) -> None:
    bundle = export("--status-category-map", "custom-status=done")
    statuses = {status["source_ref"]: status["category"] for status in bundle.lookups["statuses"]}
    assert statuses["custom-status"] == "done"


def test_a_setting_that_cannot_be_read_falls_back_to_rt_defaults(
    sample: sample_rt.SampleRT, export: Export, caplog: pytest.LogCaptureFixture
) -> None:
    sample_rt.update(sample, "Configurations", 1, content="{ 'billing' => [ 'quoted' ")
    sample_rt.insert(
        sample, "Configurations", {"id": 10, "name": "Timezone", "contenttype": "application/json", "disabled": 0,
                                   "content": "{not json"}
    )  # fmt: skip
    bundle = export()
    statuses = {status["source_ref"] for status in bundle.lookups["statuses"]}
    # RT's default lifecycle only: the billing lifecycle's statuses no ticket has are unknown.
    assert "invoiced" not in statuses and {"new", "open", "stalled"} <= statuses
    assert bundle.not_carried()["settings in RT's database that could not be read (RT's defaults used)"] == 2
    assert "Lifecycles could not be read" in caplog.text
    assert validate_bundle(bundle.root).ok


def test_rt44_has_default_lifecycle_and_priority_buckets(export: Export, tmp_path: Path) -> None:
    bundle = export(source=sample_rt.build(tmp_path / "rt-4", version="4.4"))
    assert [priority["name"] for priority in bundle.lookups["priorities"]] == ["Low", "Medium", "High"]
    issue = bundle.by_ref("issues")["1"]
    assert issue["priority_ref"] == "medium"
    assert issue["extra"]["priority_bucket"] == "Medium"
    assert "quoted" not in {status["source_ref"] for status in bundle.lookups["statuses"]}


def test_articles(bundle: Bundle) -> None:
    pages = bundle.by_ref("wiki_pages")
    support = pages["1"]
    assert support["project_ref"] == "3"  # its class applies to Support only
    assert support["title"] == "Resetting a password"
    assert [version["text"] for version in support["versions"]] == [
        "Open the **login page**.",
        "Open the **login page** and check your mail.",
    ]
    assert support["versions"][1]["author_ref"] == str(BOB)
    assert support["extra"]["summary"] == "How to reset a password"
    faq = pages["2"]
    assert faq["project_ref"] == "class:3"
    assert bundle.by_ref("projects")["class:3"]["name"] == "Articles: FAQ"
    assert bundle.not_carried()["disabled articles"] == 1


def test_counted_not_carried(bundle: Bundle) -> None:
    not_carried = bundle.not_carried()
    for what in ("assets", "scrips", "templates", "saved searches", "dashboards", "article topics"):
        assert not_carried[what] >= 1, what
    assert not_carried["copies of mail RT sent (EmailRecord transactions)"] == 1
    assert not_carried["reminders (kept in the extra of the ticket they remind of)"] == 1


def test_queue_selection_and_limit(export: Export) -> None:
    bundle = export("--queue", "support", "--limit", "2")
    assert [issue["source_ref"] for issue in bundle.get("issues")] == ["1", "2"]
    assert set(bundle.by_ref("projects")) == {"3"}
    # The class of the Support queue comes along; the global FAQ class does not.
    assert set(bundle.by_ref("wiki_pages")) == {"1"}


def test_unknown_queue_is_a_usage_error(sample: sample_rt.SampleRT, tmp_path: Path) -> None:
    argv = ["--db-url", sample.url, "--instance", "x", "--out", str(tmp_path / "b"), "--queue", "Nope", "--quiet"]
    assert main(argv) == 2


def test_same_input_same_bundle(export: Export) -> None:
    first = export("--exported-at", "2026-02-01T00:00:00Z")
    second = export("--exported-at", "2026-02-01T00:00:00Z")
    for path in sorted(first.root.rglob("*")):
        if path.is_file():
            assert path.read_bytes() == (second.root / path.relative_to(first.root)).read_bytes(), path.name


def test_an_unfinished_bundle_is_replaced(sample: sample_rt.SampleRT, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "issues.jsonl").write_text("{}\n")
    (out / "files").mkdir()
    argv = ["--db-url", sample.url, "--instance", "x", "--out", str(out), "--quiet"]
    assert main(argv) == 0
    assert read_bundle(out).manifest["counts"]["issues"] == 8


def test_a_directory_with_other_files_is_not_replaced(sample: sample_rt.SampleRT, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    argv = ["--db-url", sample.url, "--instance", "x", "--out", str(out), "--quiet"]
    assert main(argv) == 2
    assert (out / "notes.txt").read_text() == "mine"


def test_files_path_keeps_attachments_apart(sample: sample_rt.SampleRT, tmp_path: Path) -> None:
    out, files = tmp_path / "bundle", tmp_path / "files"
    argv = ["--db-url", sample.url, "--instance", "x", "--out", str(out), "--files-path", str(files), "--quiet"]
    argv += ["--external-storage-path", str(sample.storage)]
    assert main(argv) == 0
    assert not (out / "files").exists()
    assert (files / sample_rt.PDF_SHA).exists()
    assert validate_bundle(out, files_path=files).ok


def test_zip(sample: sample_rt.SampleRT, tmp_path: Path) -> None:
    out = tmp_path / "bundle.zip"
    assert main(["--db-url", sample.url, "--instance", "x", "--out", str(out), "--quiet", "--validate"]) == 0
    assert out.is_file()
