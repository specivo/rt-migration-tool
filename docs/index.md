---
description: Move queues, tickets, their history and attachments, people, custom fields and articles from Request Tracker (RT 4.4, 5.0, 6.0) into Specivo with the specivo-rt-migration tool. What it does, what you need, and where to start.
---

# RT migration tool

The RT migration tool copies queues, tickets with their whole history and attachments, people,
groups, custom fields and articles from Request Tracker into [Specivo](https://specivo.io). It reads
RT's database directly, read-only, and writes a Specivo **import bundle**: a folder (or a `.zip`) of
plain files. Specivo then checks the bundle and loads it.

```text
RT database  --SELECT-->  specivo-rt-migration  -->  import bundle  -->  Specivo
(PostgreSQL or MySQL)     (this tool)                (files)              (validate, dry run, import)
```

The tool never changes anything in RT. It reads in one read-only transaction, so the bundle shows RT
as it was at one moment, even while people keep working in it.

!!! note "Not affiliated with Best Practical"
    This tool is not affiliated with or endorsed by Best Practical Solutions. "Request Tracker" and
    "RT" are trademarks of Best Practical Solutions, LLC, used here only to say which system the tool
    reads.

## What you need

| | Requirement |
|---|---|
| **Specivo** | 0.11.0 or later, to load the bundle (bundle format 1.0). |
| **RT** | 4.4, 5.0 or 6.0, on PostgreSQL, MySQL or MariaDB. See [RT versions](versions.md). |
| **The machine that runs the tool** | Python 3.12 or later, network access to RT's database, and disk space for the attachments. |
| **A database account** | One that can `SELECT` from RT's tables. Nothing else is needed. |
| **RT's external storage** | Only if RT moved attachments out of the database (`%ExternalStorage`) with the Disk backend: read access to that directory. |

## How a migration goes

1. [Install the tool](install.md).
2. [Export](exporting.md) a trial of a few tickets per queue, then the whole installation or queue by
   queue.
3. [Check, rehearse and load the bundle](loading.md) in Specivo.
4. Read the report. Anything the tool could not carry is counted in the bundle's manifest, and
   Specivo prints it in the import report. See [What is not carried over](not-carried-over.md).
5. Export again just before you switch over, with the same `--instance`, and load the new bundle to
   add what changed since.

## In these docs

- [Installation](install.md)
- [Exporting from RT](exporting.md)
- [Validating and loading the bundle](loading.md)
- [What is carried over](carried-over.md)
- [What is not carried over](not-carried-over.md)
- [RT versions](versions.md)
- [Troubleshooting](troubleshooting.md)

For the Specivo side of an import, see [Importing from another tracker](https://specivo.io/docs/specivo/install/importing/)
in the Specivo user guide. The bundle format is specified in
[`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md) in the
Specivo repository.
