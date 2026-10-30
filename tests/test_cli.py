"""The command: options, usage errors, and the database URL kept secret."""

from __future__ import annotations

from pathlib import Path

import pytest

from specivo_rt_migration import db
from specivo_rt_migration.cli import main
from tests import sample_rt


def test_needs_a_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("RT_DATABASE_URL", raising=False)
    assert main(["--instance", "x", "--out", str(tmp_path / "b")]) == 2
    assert "RT_DATABASE_URL" in capsys.readouterr().err


def test_database_url_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = sample_rt.build(tmp_path / "rt")
    monkeypatch.setenv("RT_DATABASE_URL", source.url)
    assert main(["--instance", "x", "--out", str(tmp_path / "b"), "--quiet"]) == 0


def test_needs_an_instance(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--db-url", "postgresql://u:p@h/rt", "--out", str(tmp_path / "b")])
    assert raised.value.code == 2
    assert main(["--db-url", "postgresql://u:p@h/rt", "--instance", " ", "--out", str(tmp_path / "b")]) == 2


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("oracle://u:secret@h/rt", "PostgreSQL, MySQL or MariaDB"),
        ("not a url", "is not a database URL"),
        ("postgresql://u:secret@h", "names no database"),
        ("postgresql+psycopg2x://u:secret@h/rt", "driver"),
    ],
)
def test_bad_urls_are_usage_errors_without_the_password(
    url: str, message: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db-url", url, "--instance", "x", "--out", str(tmp_path / "b")]) == 2
    err = capsys.readouterr().err
    assert message in err
    assert "secret" not in err


def test_a_failed_connection_hides_the_password(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://rt_user:hunter2secret@127.0.0.1:1/rt"
    assert main(["--db-url", url, "--instance", "x", "--out", str(tmp_path / "b"), "--quiet"]) == 1
    err = capsys.readouterr().err
    assert "export failed" in err
    assert "hunter2secret" not in err


def test_not_an_rt_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    assert main(["--db-url", f"sqlite+aiosqlite:///{path}", "--instance", "x", "--out", str(tmp_path / "b")]) == 2
    assert "no RT tables" in capsys.readouterr().err


def test_bad_options(tmp_path: Path) -> None:
    base = ["--db-url", "postgresql://u:p@h/rt", "--instance", "x", "--out", str(tmp_path / "b")]
    for extra in (["--status-category-map", "new=nowhere"], ["--limit", "0"], ["--exported-at", "yesterday"]):
        with pytest.raises(SystemExit) as raised:
            main([*base, *extra])
        assert raised.value.code == 2


def test_bad_timezone_and_storage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = sample_rt.build(tmp_path / "rt")
    base = ["--db-url", source.url, "--instance", "x", "--quiet"]
    assert main([*base, "--out", str(tmp_path / "a"), "--timezone", "Mars/Olympus"]) == 2
    assert main([*base, "--out", str(tmp_path / "b"), "--external-storage-path", str(tmp_path / "missing")]) == 2
    err = capsys.readouterr().err
    assert "Mars/Olympus" in err and "not a directory" in err


def test_timezone_moves_dates(tmp_path: Path) -> None:
    from tests.conftest import read_bundle

    source = sample_rt.build(tmp_path / "rt")
    out = tmp_path / "b"
    assert (
        main(["--db-url", source.url, "--instance", "x", "--out", str(out), "--quiet", "--timezone", "Asia/Bangkok"])
        == 0
    )
    issue = read_bundle(out).by_ref("issues")["1"]
    # Due 2026-01-20 18:00 UTC is the 21st in Bangkok.
    assert issue["due_date"] == "2026-01-21"


def test_progress_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = sample_rt.build(tmp_path / "rt")
    out = tmp_path / "b"
    assert main(["--db-url", source.url, "--instance", "x", "--out", str(out), "--validate"]) == 0
    captured = capsys.readouterr()
    assert "Found RT 6.0" in captured.err
    assert "Queue Support: 4 tickets" in captured.err
    assert "custom-status" in captured.err
    assert "The bundle is valid." in captured.out


def test_url_handling() -> None:
    assert db.normalise_source_url("postgresql://u:p@h/rt").drivername == "postgresql+asyncpg"
    assert db.normalise_source_url("mariadb://u:p@h/rt").drivername == "mysql+aiomysql"
    assert db.normalise_source_url("pg://u:p@h/rt").password == "p"
    assert db.mask_password("failed for u:s3cr3t@h", "postgresql://u:s3cr3t@h/rt") == "failed for u:***@h"
    assert db.safe_url("postgresql://u:s3cr3t@h/rt") == "postgresql://u:***@h/rt"


def test_tables_are_named_per_database() -> None:
    assert db.tables_for("mysql").Tickets.name == "Tickets"
    assert db.tables_for("postgresql").Tickets.name == "tickets"
    assert db.tables_for("postgresql").Tickets.c.effectiveid is not None
