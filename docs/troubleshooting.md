---
description: Fix common problems with specivo-rt-migration and the Specivo import - connection errors, unsupported RT versions, missing attachments, garbled text, unknown statuses, stopped runs and warnings.
---

# Troubleshooting

## The tool will not start the export

| Message | What it means |
|---|---|
| `give --db-url, or set RT_DATABASE_URL` | No database. |
| `--db-url is not a database URL` | The URL does not parse. Write it as `postgresql://USER:PASSWORD@HOST:PORT/DATABASE`; percent-encode special characters in the password. |
| `--db-url names a ... database` | Only PostgreSQL, MySQL and MariaDB are read. RT on Oracle or SQLite is not supported. |
| `the following arguments are required: --instance` | Name the installation, see [Exporting](exporting.md#a-first-trial-export). |
| `No queue 'X' in RT` | `--queue` names a queue RT does not have. Give its exact name or its id. |
| `this database has no RT tables` | The URL names another database than RT's. |
| `this is an RT older than 4.4` | Upgrade RT to 4.4 or later first, with RT's own upgrade tools. |
| `... already exists: choose a new path` | `--out` points at a finished bundle or a folder with other files in it. Use a new path. |
| `--external-storage-path ... is not a directory` | The path to RT's external storage is wrong. |
| `--timezone ... is not a time zone name` | Use a name such as `Asia/Bangkok` or `Europe/Berlin`. |

## The export fails

`export failed: ...` with exit status 1 means the export could not finish; the message says why (the
password is never shown). Common causes:

- **Connection refused, timeout, authentication failed.** Check the host, port and account, and that
  the machine may reach the database port. On MySQL, the account must be allowed from the machine
  the tool runs on (`'rt_export'@'%'`, or that machine's address).
- **The connection was dropped** during a long export. Run the same command again: it replaces the
  unfinished bundle. Export from a replica, or with a database idle timeout long enough.
- **Permission denied for table ...**: the account cannot read one of RT's tables. Grant `SELECT` on
  all of them.

## Attachments are missing

The manifest says how many and why:

- *files in RT's external storage, not read*: give `--external-storage-path` (Disk backend), or copy
  the files from another backend into the Disk layout first. See
  [Exporting](exporting.md#attachments-in-external-storage).
- *files missing from RT's external storage*: the directory is not the backend's root, or lacks
  files. Each file is at `<root>/<3 hex>/<3 hex>/<rest of the SHA-256>`.
- *files that could not be decoded*: RT's content is damaged. The record of the message is still
  carried.

## Text looks wrong

- **Garbled characters** (`à¸ à¸²à¸©à¸²`) in old messages: RT stored them in a charset the part did not
  declare. The tool tries UTF-8 first, then the declared charset; there is nothing else to go by.
- **On MySQL, every non-Latin character is `?`**: the database itself holds question marks, from an
  RT that once wrote to a `latin1` table. Check in RT; the tool cannot restore them.
- **Plain text shows backslashes in the bundle** (`\*`, `\#`): that is how Markdown keeps characters
  from being read as formatting. Specivo shows the text as it was.
- **A number became a link it should not be**, or the other way round: see the rules in
  [References](carried-over.md#references-to-other-tickets). A bare `#123` is linked only with
  `--link-hash-numbers`.

## Statuses or priorities are not what RT shows

- The tool lists statuses no lifecycle in RT's database describes, and places them by name. Set them
  with `--status-category-map`. On RT 4.4 every lifecycle except RT's default is like this.
- Priority names come from RT 5.0 and 6.0's database settings, or RT's defaults (Low, Medium, High).
  Names set only in `RT_SiteConfig.pm` cannot be read; the number is kept in each issue's `extra`.

## Specivo reports warnings

| Warning | What to do |
|---|---|
| `external_ref` on issues | References or links to tickets of another bundle of the same RT. Load that bundle too, with `--continue-import`. |
| Users without an email address | RT users without one get a placeholder address in Specivo. |
| Accounts that are not active | RT's unprivileged and disabled users, imported locked. |
| A login already taken | Specivo adds a suffix and lists it. |

## Running it again

An export never changes RT, and exporting again is always safe. Loading a bundle again creates
nothing that is already there, as long as the `--instance` is the same.
