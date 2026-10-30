"""End to end: a real RT, filled by fixture/seed.pl, exported and validated.

Runs against the fixture ``make fixture-up fixture-seed fixture-files`` started,
through ``make fixture-test``, which says which one in the variables below.
Skipped without them, or when that fixture is not running, unless
``RT_FIXTURE_REQUIRED`` is set, as in CI, where a fixture that cannot be
reached is a failure.

The checks name what the seed script creates: change one, change the other.
"""

from __future__ import annotations

import os
import socket
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest
from specivo_migration_sdk import validate_bundle

from specivo_rt_migration.cli import main
from tests.conftest import Bundle, read_bundle

pytestmark = pytest.mark.fixture

VERSION = os.environ.get("RT_FIXTURE_VERSION", "6.0.4")
LINE = ".".join(VERSION.split(".")[:2])
DB_URL = os.environ.get("RT_FIXTURE_DB_URL", "")
FILES = Path(os.environ.get("RT_FIXTURE_FILES", f".fixture-files/{VERSION}-pg"))


def _reachable() -> bool:
    parsed = urlparse(DB_URL)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5432), timeout=1):
            return True
    except OSError:
        return False


def _export(tmp_path_factory: pytest.TempPathFactory, *extra: str) -> Bundle:
    if not DB_URL:
        pytest.skip("no RT fixture named (make fixture-test RT_VERSION=... DB=...)")
    if not _reachable():
        if os.environ.get("RT_FIXTURE_REQUIRED"):
            pytest.fail(f"the RT fixture at {urlparse(DB_URL).netloc.split('@')[-1]} cannot be reached")
        pytest.skip("no RT fixture running (make fixture-up fixture-seed fixture-files)")
    out = tmp_path_factory.mktemp("e2e") / "bundle"
    argv = ["--db-url", DB_URL, "--instance", "fixture.example.com", "--out", str(out), "--quiet"]
    argv += ["--external-storage-path", str(FILES), *extra]
    assert main(argv) == 0
    return read_bundle(out)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    return _export(tmp_path_factory)


@pytest.fixture(scope="module")
def with_comment_files(tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    return _export(tmp_path_factory, "--include-comment-files")


def _issue(bundle: Bundle, subject_start: str) -> dict[str, object]:
    return next(issue for issue in bundle.get("issues") if str(issue["subject"]).startswith(subject_start))


def test_valid_with_counts(bundle: Bundle) -> None:
    report = validate_bundle(bundle.root)
    assert report.ok, report.format_text()
    assert bundle.manifest["source"]["version"] == LINE
    counts = bundle.manifest["counts"]
    # Ten tickets: two merged into the first, one deleted; the reminder and the approval are not tickets.
    assert counts["issues"] == 7
    assert counts["users"] == 7
    assert counts["groups"] == 4  # three of RT's, and the one for its privileged users
    assert counts["wiki_pages"] == 2
    assert counts["time_entries"] == 4


def test_first_ticket(bundle: Bundle) -> None:
    issue = _issue(bundle, "Login page is broken after the upgrade")
    extra = issue["extra"]
    assert isinstance(extra, dict)
    assert extra["merged_tickets"] == [4, 5]
    assert extra["requestors"] == ["dana@example.net"]
    assert extra["reminders"][0]["subject"] == "Call the customer back"
    description = str(issue["description"])
    assert "**login page**" in description
    assert "เข้าสู่ระบบไม่ได้" in description
    assert "{{attachment:" in description and "alert" not in description
    values = {value["key"]: value["value"] for value in issue["custom_values"]}  # type: ignore[attr-defined]
    assert values["severity"] == "Critical"
    assert values["components"] == ["Web"]
    assert values["server_address"] == "192.0.2.10"
    assert values["network_range"] == "198.51.100.0-198.51.100.255"
    assert values["go_live_date"] == "2026-12-01"


def test_history(bundle: Bundle) -> None:
    first = str(_issue(bundle, "Login page is broken after the upgrade")["source_ref"])
    journals = [journal for journal in bundle.get("journals") if journal["issue_ref"] == first]
    notes = [str(journal.get("notes") or "") for journal in journals]
    assert "ขอบคุณครับ ตอนนี้ใช้งานได้แล้ว" in notes
    assert any(note.startswith("We are *on it*") for note in notes)
    assert any(journal.get("is_private") for journal in journals)
    keys = {detail["prop_key"] for journal in journals for detail in journal.get("details", [])}
    assert {"subject", "status_id", "assigned_to_id", "project_id", "due_date", "admincc"} <= keys
    # Text from the merged tickets is on the survivor.
    assert any(journal.get("extra", {}).get("merged_from") for journal in journals)


def test_files(bundle: Bundle) -> None:
    names = {attachment["filename"] for attachment in bundle.get("attachments")}
    assert {"error.png", "invoice.pdf", "second.png", "login-error.png", "contract.pdf"} <= names
    pdf = next(attachment for attachment in bundle.get("attachments") if attachment["filename"] == "invoice.pdf")
    assert pdf["filesize"] > 4096  # in RT's external storage
    # The files of the two comments are left out: Specivo attachments are not private.
    assert not {"server.log", "mail.bin"} & names
    not_carried = {item["what"]: item.get("count") for item in bundle.manifest["not_carried_over"]}
    assert not_carried["files of private comments (--include-comment-files carries them)"] == 2


def test_files_of_comments_when_asked(with_comment_files: Bundle) -> None:
    names = {attachment["filename"] for attachment in with_comment_files.get("attachments")}
    # mail.bin was added after the files were moved to external storage: it is in the database.
    assert {"server.log", "mail.bin"} <= names


def test_links(bundle: Bundle) -> None:
    first = str(_issue(bundle, "Login page is broken after the upgrade")["source_ref"])
    invoice = _issue(bundle, "Invoice for the login fix")
    assert invoice["parent_ref"] == first
    assert invoice["description"] == "Bill the work on {{issue:" + first + "}}, see also {{issue:2}}."
    kinds = {relation["relation_type"] for relation in bundle.get("relations")}
    assert kinds == {"blocks", "relates"}


def test_people_and_access(bundle: Bundle) -> None:
    users = {user["login"]: user for user in bundle.get("users")}
    assert "RT_System" not in users and "Nobody" not in users
    assert users["alice"]["is_admin"] is True
    assert users["dana@example.net"]["status"] == "locked"
    assert users["carol"]["status"] == "locked"
    assert users["somchai"]["display_name"] == "สมชาย ใจดี"
    groups = {group["source_ref"]: group["name"] for group in bundle.get("groups")}
    projects = {project["source_ref"]: project["name"] for project in bundle.get("projects")}
    roles = {
        (projects[m["project_ref"]], groups.get(m["principal_ref"]) if m["principal_kind"] == "group" else
         next(u["login"] for u in bundle.get("users") if u["source_ref"] == m["principal_ref"])): m["role_refs"][0]
        for m in bundle.get("memberships")
    }  # fmt: skip
    assert roles[("Support", "Support Team")] == "developer"
    assert roles[("Billing", "bob")] == "manager"
    assert roles[("Support", "somchai")] == "developer"
    assert roles[("งานภาษาไทย", "Thai Desk")] == "reporter"
    # RT's Privileged group may see General: one group of every privileged user, a Reporter there.
    privileged = next(group for group in bundle.get("groups") if group["name"] == "RT privileged users")
    logins = {user["source_ref"]: user["login"] for user in bundle.get("users")}
    assert {logins[ref] for ref in privileged["member_refs"]} == {"root", "alice", "bob", "somchai", "carol"}
    assert roles[("General", "RT privileged users")] == "reporter"
    assert [m["project_ref"] for m in bundle.get("memberships") if m["principal_ref"] == privileged["source_ref"]] == [
        next(ref for ref, name in projects.items() if name == "General")
    ]
    # RT's Everyone group is never carried: what it holds is named in the report and each project's extra.
    whats = [item["what"] for item in bundle.manifest["not_carried_over"]]
    assert "rights granted to RT's Everyone group on every queue: CreateTicket, ReplyToTicket, SeeCustomField" in whats
    for project in bundle.get("projects"):
        if "rt_queue" in project["extra"]:
            assert project["extra"]["rt_everyone_rights"] == ["CreateTicket", "ReplyToTicket", "SeeCustomField"]


def test_statuses_and_priorities(bundle: Bundle) -> None:
    statuses = {status["name"]: status["category"] for status in bundle.lookups["statuses"]}
    assert statuses["new"] == "backlog" and statuses["resolved"] == "closed"
    priorities = {priority["name"] for priority in bundle.lookups["priorities"]}
    if LINE == "4.4":
        assert "quoted" not in statuses
        assert priorities == {"Low", "Medium", "High"}
    else:
        assert statuses["quoted"] == "backlog" and statuses["paid"] == "closed"
        assert priorities == {"Low", "Medium", "High", "Routine", "Urgent"}


def test_time(bundle: Bundle) -> None:
    entries = bundle.get("time_entries")
    assert sum(Decimal(str(entry["hours"])) for entry in entries) == Decimal("1.83")
    alice = next(user["source_ref"] for user in bundle.get("users") if user["login"] == "alice")
    worked = [entry for entry in entries if entry["spent_on"] == "2026-10-01"]
    if LINE == "6.0":
        assert len(worked) == 1 and worked[0]["user_ref"] == alice
    else:
        assert worked == []


def test_articles(bundle: Bundle) -> None:
    pages = {page["title"]: page for page in bundle.get("wiki_pages")}
    reset = pages["Resetting a password"]
    assert len(reset["versions"]) == 2
    assert "check your mail" in reset["versions"][-1]["text"]
    projects = {project["source_ref"]: project for project in bundle.get("projects")}
    assert projects[reset["project_ref"]]["name"] == "Support"
    thai = pages["วิธีติดต่อฝ่ายบริการ"]
    assert projects[thai["project_ref"]]["name"] == "Articles: FAQ"


def test_not_carried(bundle: Bundle) -> None:
    not_carried = {item["what"]: item.get("count") for item in bundle.manifest["not_carried_over"]}
    assert not_carried["deleted tickets"] == 1
    assert not_carried["approval tickets"] == 1
    assert not_carried["assets"] == 1
    assert not_carried["custom fields of users"] == 1
    assert not_carried["disabled articles"] == 1
