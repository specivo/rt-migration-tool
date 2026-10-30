---
description: Export Request Tracker to a Specivo import bundle with specivo-rt-migration - the database URL, the instance name, a trial run, choosing queues, external storage, time zones, large migrations and exporting again.
---

# Exporting from RT

## The database

Give the tool RT's database as a URL, preferably in the `RT_DATABASE_URL` environment variable, so
the password stays out of the process list and the shell history:

```bash
export RT_DATABASE_URL='postgresql://rt_user:PASSWORD@db.example.com:5432/rt'
# MySQL or MariaDB:
export RT_DATABASE_URL='mysql://rt_user:PASSWORD@db.example.com:3306/rt'
```

The values are those of RT's `$DatabaseHost`, `$DatabasePort`, `$DatabaseName`, `$DatabaseUser` and
`$DatabasePassword` settings. `postgres://`, `mariadb://` and the names of Python drivers are
accepted too; the tool picks its own driver. A password with special characters must be
percent-encoded in the URL (`@` is `%40`).

The account needs only `SELECT` on RT's tables. On PostgreSQL:

```sql
CREATE ROLE rt_export LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE rt TO rt_export;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rt_export;
```

On MySQL or MariaDB: `GRANT SELECT ON rt.* TO 'rt_export'@'%' IDENTIFIED BY '...'` (or `CREATE USER`
then `GRANT` on MySQL 8).

## A first, trial export

```bash
specivo-rt-migration --instance rt.example.com --out rt-trial --limit 20 --validate
```

- `--instance` names this RT installation in the bundle, such as its web host name. It is required,
  and must be the same on every export of one installation: it is how Specivo knows what it imported
  before. Two different RT installations need two different names.
- `--limit 20` exports at most 20 tickets of each queue.
- `--validate` checks the bundle once written, as Specivo will.

The tool prints what it does as it goes:

```text
Reading postgresql+asyncpg://rt_user:***@db.example.com:5432/rt
Found RT 5.0
Tickets to export: 140 in 5 queues
Statuses: 10 (lifecycles from RT's settings), priorities: 5
Users: 212 (180 unprivileged), groups: 6
Memberships: 31
Custom fields: 12
Relations: 18, parents: 4
Queue Support: 100 tickets
...
Articles: 25 wiki pages
```

## The whole installation, or some queues

Without `--queue`, every queue is exported. `--queue NAME` (by name or id, repeatable) exports only
those:

```bash
specivo-rt-migration --instance rt.example.com --out rt-support --queue Support --queue Billing
```

Export the remaining queues later into bundles of their own, with the same `--instance`. Links and
references between tickets of different bundles are written so that Specivo makes them once both
bundles are loaded, in either order (load every bundle after the first with `--continue-import`).
Each bundle carries the users its queues name (the people of their tickets, history and articles,
and the members of their projects) and the groups that are members of their projects; lookups are in
every bundle. Specivo adds each user and group once, however many bundles carry it. Article classes
that apply to one of the selected queues come with it; the others come with a full export.

## Attachments in external storage

RT can move attachments out of the database (`%ExternalStorage`, with `rt-externalize-attachments`).
The database then holds only each file's SHA-256. With the **Disk** backend, give the tool the
backend's root directory (its `Path` setting), or a copy of it:

```bash
specivo-rt-migration --instance rt.example.com --out rt-export \
    --external-storage-path /opt/rt5/var/attachments
```

The tool reads `<root>/<3 hex digits>/<3 hex digits>/<the rest>`, as RT stores it, and checks each
file's hash. Without the option, or with another backend (Amazon S3, Dropbox), those files are left
out and counted in the manifest. To carry files from another backend, copy them into the Disk layout
first.

## Time zone

RT stores times in UTC. Due dates, start dates and the day time was worked are dates in Specivo,
taken in RT's time zone when RT 5.0 or 6.0 keeps it in the database, else in UTC. If RT's `$Timezone`
is set in `RT_SiteConfig.pm`, give it:

```bash
specivo-rt-migration ... --timezone Asia/Bangkok
```

## Users who are not staff

RT creates an unprivileged user for everyone who mails it. They are carried, so every message keeps
its author, but **locked**: they cannot sign in to Specivo. To let them sign in (a customer portal,
say), add `--activate-unprivileged`.

## Files of comments

A comment in RT is seen by staff only. Specivo carries it as a private note, but its files would be
attachments of the issue, which have no private flag, so the tool leaves them out and counts them.
If everyone who will see the issues in Specivo may see those files too, carry them with
`--include-comment-files`: they then become files of the issue, **visible to everyone who can see
the issue**, requestors included.

## Statuses RT's database does not describe

Statuses from a lifecycle defined in `RT_SiteConfig.pm` (and every lifecycle on RT 4.4) are placed by
their name. The tool lists those it had to guess when it finishes; place them yourself with:

```bash
specivo-rt-migration ... --status-category-map 'waiting=active,cancelled=closed'
```

## Large migrations

The bundle is a **directory** by default: records in `.jsonl` files and each attachment once, by
content, under `files/`. For a large installation, keep the attachments out of the bundle with
`--files-path`:

```bash
specivo-rt-migration --instance rt.example.com --out rt-export --files-path rt-files
```

Copy both folders to the Specivo server with `rsync`, which resumes an interrupted copy, and load
with Specivo's `--files-path` (see [Loading](loading.md)). A later export into the same
`--files-path` writes only files that are not there yet.

`--zip` (or an `--out` ending in `.zip`) writes a single archive instead, handy for a trial.

## If an export stops

An export that stops (a lost connection, Ctrl-C) leaves a bundle without its manifest, which Specivo
refuses to load. Run the same command again: it replaces the unfinished bundle (and nothing else: a
folder holding other files is refused), and with `--files-path` the attachment files already written
are kept.

## The same bundle again

The export reads in a fixed order, so the same RT gives the same bundle. Only the export time
differs; `--exported-at 2026-03-01T00:00:00Z` sets it, for a bundle identical byte for byte to an
earlier one.

## Exporting again before the switch

Export again just before switching over, with the **same `--instance`**, into a new folder. Loading
it adds only what is new since the first import: Specivo recognises every record it imported before
by its RT id.

## All options

Run `specivo-rt-migration --help`, or see the [README](https://github.com/specivo/rt-migration-tool#options).
