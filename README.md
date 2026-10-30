# specivo-rt-migration

Exports a Request Tracker (RT) installation to a [Specivo](https://github.com/specivo/specivo)
import bundle. It reads RT's database (PostgreSQL, MySQL or MariaDB), and RT's Disk external storage
when attachments were moved out of the database, converts message text to Markdown, and writes the
bundle with [`specivo-migration-sdk`](https://github.com/specivo/migration-sdk). Specivo then
validates the bundle and loads it.

This tool is not affiliated with or endorsed by Best Practical Solutions. "Request Tracker" and
"RT" are trademarks of Best Practical Solutions, LLC, used here only to say which system the tool
reads. The tool only reads RT's data; it contains no part of RT.

- Bundle format: v1. Requires Specivo 0.11.0 or later to load.
- SDK: [`specivo-migration-sdk`](https://github.com/specivo/migration-sdk) `v0.2.0`.
- Reads RT 4.4, 5.0 and 6.0. Tested against RT 4.4.9, 5.0.11 and 6.0.4, each on PostgreSQL 16 and
  MySQL 8.4. Python 3.12 or later.

The full documentation is in [`docs/`](docs/index.md): installing, exporting, what is carried over
and what is not, the differences between RT versions, loading the bundle, and troubleshooting.

## What it carries

- queues as projects (a disabled queue archived), with memberships derived from the rights RT
  grants on each queue: Manager, Developer or Reporter;
- tickets as issues, with their description (the first message), status, priority, owner,
  requestors and watchers, dates, estimates, custom field values, and their whole history: replies
  (public notes), comments (private notes; their files only when asked, as Specivo files are not
  private), field changes, files, and time worked as time entries;
- merged tickets folded into the ticket they were merged into, so their messages and files stay
  together and references to their numbers still lead somewhere;
- links: parent and child, depends on (blocks), refers to (relates), across exported parts too;
- references to other tickets in text (`ticket 123`, `RT #123`, `[example.com #123]`), as links to
  the issue the ticket became;
- users (privileged ones active, unprivileged ones locked unless asked otherwise), user-defined
  groups with their members, and every ticket custom field with its history;
- articles as wiki pages with every revision.

Everything RT has that the bundle has no place for is counted in the manifest's
`not_carried_over`, which Specivo prints in its import report: assets, scrips, templates, saved
searches, dashboards, service levels, reminders and approvals (a ticket's reminders are kept in its
`extra`), RT's access rights themselves, and custom fields on anything but tickets. See
[What is carried over](docs/carried-over.md) and [What is not](docs/not-carried-over.md).

## Install

From a release (there is no PyPI package):

```bash
uv tool install git+https://github.com/specivo/rt-migration-tool@vX.Y.Z
# or: pipx install git+https://github.com/specivo/rt-migration-tool@vX.Y.Z
```

## Use

Run it where it can reach RT's database. The database password is best passed in the
environment, so it does not show in the process list:

```bash
export RT_DATABASE_URL='postgresql://rt_user:PASSWORD@db.example.com/rt'   # or mysql://...
specivo-rt-migration --instance rt.example.com --out rt-export --validate
```

Give it a database account that can only `SELECT`. Everything is read in one read-only,
repeatable-read transaction, so the bundle shows RT as it was at one moment even while people keep
working in it; export from a replica or at a quiet hour on a large installation.

Then, on the Specivo server:

```bash
python -m specivo.cli.import_data --source bundle --path rt-export --validate
python -m specivo.cli.import_data --source bundle --path rt-export --dry-run
python -m specivo.cli.import_data --source bundle --path rt-export
```

### Options

| Option | Meaning |
|---|---|
| `--db-url URL` | RT's database (default: `$RT_DATABASE_URL`). `postgresql://`, `mysql://` or `mariadb://`; the async driver is chosen for you. |
| `--instance NAME` | Required. The installation's name in the bundle, such as its web host name. Use the same one on every export. |
| `--queue NAME` | Only this queue (name or id). Repeatable. |
| `--external-storage-path DIR` | The root of RT's Disk external storage, for attachments RT moved out of the database. |
| `--out PATH` | A new or empty directory, or a `.zip` path. |
| `--zip` | Write a `.zip` instead of a directory. |
| `--files-path DIR` | Keep the attachment files in this directory, outside the bundle. |
| `--validate` | Check the bundle once written, as Specivo will. |
| `--exported-at TIME` | The export time to record, to get a bundle identical to an earlier one. |
| `--activate-unprivileged` | Import unprivileged users (requestors) as active accounts instead of locked ones. |
| `--status-category-map STATUS=CATEGORY,...` | Place statuses in Specivo's backlog/active/done/closed grouping. |
| `--cf-key-map NAME=KEY,...` | Override the metadata key derived from a custom field's name. |
| `--timezone ZONE` | The time zone for dates (due, starts, the day time was worked). Default: RT's, or UTC. |
| `--link-hash-numbers` | Also link a bare `#123` in text. |
| `--include-comment-files` | Also carry the files of comments (private notes). Specivo attachments have no private flag: the files are then visible to everyone who can see the issue. |
| `--limit N` | At most N tickets per queue, for a trial run. |
| `--batch-size N` | Tickets read per batch (default: 100). |
| `--quiet` | Print only errors. Progress goes to stderr, the bundle's path to stdout. |

Exit status: 0 on success, 1 when the export failed or `--validate` found problems, 2 for a usage
error. An export that stops half way leaves a bundle without a manifest, which Specivo refuses;
running the same command again replaces it.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
```

The SDK is pinned to a release tag of [`specivo/migration-sdk`](https://github.com/specivo/migration-sdk).

Most tests run the whole export against a small RT database built in SQLite (`tests/sample_rt.py`),
once for each RT version's schema. The end-to-end test runs it against a real RT.

### The RT fixture

There is no official image of RT itself. `fixture/Dockerfile` builds each RT release from its git
tag, pinned by the commit the tag points at, on Best Practical's image of RT's dependencies
(`bpssysadmin/rt-base-debian`, pinned by digest), the image RT's own repository tests with.
`docker-compose.fixture.yml` runs it on PostgreSQL (`DB=pg`, the default) or MySQL (`DB=mysql`), and
`fixture/seed.pl` fills it through RT's own Perl API.

```bash
make fixture-build RT_VERSION=6.0.4          # build RT 6.0.4 (or 4.4.9, 5.0.11)
make fixture-up RT_VERSION=6.0.4 DB=pg       # start it
make fixture-seed RT_VERSION=6.0.4 DB=pg     # fill it, and move large files to external storage
make fixture-files RT_VERSION=6.0.4 DB=pg    # copy the external storage out to .fixture-files/
make fixture-test RT_VERSION=6.0.4 DB=pg     # the end-to-end test, failing (not skipping) without the fixture
make fixture-export RT_VERSION=6.0.4 DB=pg   # write and validate out/fixture-6.0.4-pg
make fixture-down RT_VERSION=6.0.4 DB=pg     # stop it; data and files are discarded
```

Building behind a proxy: `FIXTURE_BUILD_OPTS='--network host --build-arg https_proxy=...'`.

Host ports, bound to 127.0.0.1 and each overridable with the variable shown:

| Service | Port | Variable |
|---|---|---|
| PostgreSQL | 5462 | `RT_TOOL_FIXTURE_PG_PORT` |
| MySQL | 3319 | `RT_TOOL_FIXTURE_MYSQL_PORT` |

Each checkout, RT version and database has a fixture of its own: the Makefile names the compose
project after the checkout's path, the version and the database, the databases are in tmpfs, and
RT's external storage is a named volume of that project, copied out with `docker compose cp`.

The CI workflow (`.github/workflows/ci.yml`) runs the end-to-end
test for every RT version on both databases, with the built RT images cached.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The fixture's seed script and Dockerfile
are this project's own; the RT they build is downloaded from RT's public repository at build time
and is licensed under the GNU General Public License, version 2.
