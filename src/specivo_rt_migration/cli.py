"""The ``specivo-rt-migration`` command.

Reads a Request Tracker database (and, when RT keeps attachments outside it,
its Disk external storage) and writes a Specivo import bundle: a directory by
default, so attachments stay plain files that ``rsync`` can copy and resume,
or a ``.zip`` with ``--zip``.

Exit status: 0 when the bundle was written (and, with ``--validate``, is
valid); 1 when the export failed or the bundle is not valid; 2 for a usage
error, such as a missing ``--instance``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from specivo_migration_sdk import timestamp, validate_bundle

from specivo_rt_migration import db
from specivo_rt_migration.export import ExportError, ExportOptions, export_bundle, generator_version
from specivo_rt_migration.schema import UnsupportedSchemaError

DB_URL_ENV = "RT_DATABASE_URL"
_STATUS_CATEGORIES = ("backlog", "active", "done", "closed")


def _pairs(value: str) -> dict[str, str]:
    """Parse ``name=value,name=value``."""
    result: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        name, sep, mapped = item.partition("=")
        if not sep or not name.strip() or not mapped.strip():
            raise argparse.ArgumentTypeError(f"expected name=value pairs, got {item!r}")
        result[name.strip()] = mapped.strip()
    return result


def _status_map(value: str) -> dict[str, str]:
    pairs = _pairs(value)
    for name, category in pairs.items():
        if category not in _STATUS_CATEGORIES:
            raise argparse.ArgumentTypeError(f"{name}: {category!r} is not one of {', '.join(_STATUS_CATEGORIES)}")
    return pairs


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _timestamp(value: str) -> str:
    try:
        return timestamp(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specivo-rt-migration",
        description=(
            "Export a Request Tracker installation (its database) to a Specivo import bundle. "
            "Load the bundle with: python -m specivo.cli.import_data --source bundle --path OUT"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {generator_version()}")
    source = parser.add_argument_group("source")
    source.add_argument(
        "--db-url",
        help=(
            "RT's database, as a URL: postgresql://user:password@host/rt or mysql://user:password@host/rt. "
            f"Defaults to ${DB_URL_ENV}, which keeps the password out of the process list."
        ),
    )
    source.add_argument(
        "--instance",
        required=True,
        help=(
            "Name of this RT installation in the bundle, such as its web host name. Use the same name on every "
            "export of one installation: it is how Specivo knows what it imported before."
        ),
    )
    source.add_argument(
        "--queue",
        action="append",
        default=[],
        metavar="NAME",
        help="Export only this queue (name or id). Repeatable. Default: every queue.",
    )
    source.add_argument(
        "--external-storage-path",
        type=Path,
        metavar="DIR",
        help=(
            "The root of RT's Disk external storage (%%ExternalStorage Path), for attachments RT moved out of the "
            "database. Without it they are left out and counted."
        ),
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out", type=Path, required=True, help="Where to write the bundle: a new or empty directory.")
    output.add_argument("--zip", action="store_true", help="Write a .zip instead of a directory.")
    output.add_argument(
        "--files-path",
        type=Path,
        metavar="DIR",
        help=(
            "Keep attachment files in this directory instead of the bundle (load with Specivo's --files-path). "
            "Files already there are kept, so a later export writes only new ones."
        ),
    )
    output.add_argument("--validate", action="store_true", help="Check the bundle once written, as Specivo will.")
    output.add_argument(
        "--exported-at",
        type=_timestamp,
        metavar="TIME",
        help="The export time to record (ISO 8601 with an offset), for a bundle identical to an earlier one.",
    )
    output.add_argument("--quiet", action="store_true", help="Print nothing but errors.")

    mapping = parser.add_argument_group("mapping")
    mapping.add_argument(
        "--activate-unprivileged",
        action="store_true",
        help="Import unprivileged users (requestors, mail senders) as active accounts instead of locked ones.",
    )
    mapping.add_argument(
        "--status-category-map",
        type=_status_map,
        default={},
        metavar="STATUS=CATEGORY,...",
        help="Place statuses in Specivo's backlog/active/done/closed grouping (default: from RT's lifecycles).",
    )
    mapping.add_argument(
        "--cf-key-map",
        type=_pairs,
        default={},
        metavar="NAME=KEY,...",
        help="Override the metadata key derived from a custom field's name.",
    )
    mapping.add_argument(
        "--timezone",
        metavar="ZONE",
        help="The time zone dates (due, starts, time worked) are taken in, e.g. Asia/Bangkok (default: RT's, or UTC).",
    )
    mapping.add_argument(
        "--link-hash-numbers",
        action="store_true",
        help="Also link a bare #123 in text to ticket 123 (off by default: '#2' is often not a ticket).",
    )
    mapping.add_argument(
        "--include-comment-files",
        action="store_true",
        help=(
            "Carry the files of comments (private notes) too. Specivo attachments have no private flag: "
            "the files are then visible to everyone who can see the issue. Default: left out and counted."
        ),
    )

    run = parser.add_argument_group("run")
    run.add_argument("--limit", type=_positive, help="Export at most this many tickets per queue, for a trial run.")
    run.add_argument("--batch-size", type=_positive, default=100, help="Tickets read per batch (default: 100).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    def usage(message: str) -> int:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {message}", file=sys.stderr)
        return 2

    db_url = args.db_url or os.environ.get(DB_URL_ENV)
    if not db_url:
        return usage(f"give --db-url, or set {DB_URL_ENV}")
    try:
        db.check_source_url(db_url)
    except db.SourceURLError as exc:
        return usage(f"--db-url {exc}")
    instance = (args.instance or "").strip()
    if not instance:
        return usage("--instance cannot be empty")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    options = ExportOptions(
        db_url=db_url,
        out=args.out,
        instance=instance,
        queues=args.queue,
        limit=args.limit,
        zip=True if args.zip else None,
        files_path=args.files_path,
        external_storage=args.external_storage_path,
        activate_unprivileged=args.activate_unprivileged,
        status_category_map=args.status_category_map,
        cf_key_map=args.cf_key_map,
        timezone=args.timezone,
        link_hash_numbers=args.link_hash_numbers,
        include_comment_files=args.include_comment_files,
        exported_at=args.exported_at,
        batch_size=args.batch_size,
    )
    try:
        summary = asyncio.run(export_bundle(options, progress))
    except (ExportError, UnsupportedSchemaError) as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{parser.prog}: interrupted; run the same command again to start over", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - one line, never a traceback holding the URL
        print(f"{parser.prog}: export failed: {_redacted(exc, db_url)}", file=sys.stderr)
        print("Run the same command again: it replaces the unfinished bundle.", file=sys.stderr)
        return 1

    if not args.quiet:
        print(summary.path)
        counts = ", ".join(f"{key} {value}" for key, value in summary.counts.items() if value)
        progress(f"Records: {counts}")
        if summary.attachment_bytes:
            progress(f"Attachment files: {summary.attachment_bytes / (1024 * 1024):.1f} MB (each content stored once)")
        if summary.guessed_statuses:
            progress(
                "Statuses no lifecycle in the database defines, placed by name: "
                + ", ".join(summary.guessed_statuses)
                + " (set them with --status-category-map)"
            )
        if summary.not_carried_over:
            progress(f"Not carried over: {len(summary.not_carried_over)} kinds of data, listed in the manifest")

    if args.validate:
        report = validate_bundle(summary.path, files_path=args.files_path)
        print(report.format_text())
        if not report.ok:
            return 1
        print("The bundle is valid.")
    return 0


def _redacted(exc: BaseException, db_url: str) -> str:
    """Return *exc* as one line of text with the database password masked. Never raises."""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 - an exception whose str() fails still gets reported
        text = type(exc).__name__
    return db.mask_password(" ".join(text.split()), db_url)
