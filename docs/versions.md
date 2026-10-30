---
description: How specivo-rt-migration tells RT 4.4, 5.0 and 6.0 apart from their database schema, and every difference between them that the export relies on.
---

# RT versions

The tool reads **RT 4.4, 5.0 and 6.0**, on PostgreSQL and on MySQL or MariaDB. It is tested against
RT 4.4.9, 5.0.11 and 6.0.4. RT keeps no version number in its database, so the tool reads the schema
(which tables and columns exist) and says which line it found when it starts:

```text
Found RT 5.0
```

It reads only the columns the database has, so nothing RT 6.0 added is ever asked of an RT 4.4
database. A database between two lines (an upgrade that stopped half way, a development release) is
described by what it has. An RT older than 4.4 is refused: upgrade it first.

## How the version is found

| Found | Means |
|---|---|
| No `Tickets`, `Transactions` or `Attachments` table | Not an RT database. Refused. |
| No `CustomRoles` table, or no `Tickets.IsMerged` or `Tickets.SLA` column | RT older than 4.4. Refused. |
| `Transactions.TimeWorker` and `Tickets.Description`, or a `SavedSearches` or `ObjectContents` table | RT 6.0 |
| A `Configurations` table | RT 5.0 |
| Otherwise | RT 4.4 |

## Differences the export relies on

### Settings kept in the database (5.0 and later)

RT 5.0 added the `Configurations` table, where RT keeps a setting changed in its web interface (the
value written by Perl's `Data::Dumper`, or as JSON). The tool reads three of them:

| Setting | Used for | Without it (RT 4.4, or not changed in the web interface) |
|---|---|---|
| `Lifecycles` | The statuses of each queue and their class (initial, active, inactive) | RT's default lifecycle: `new`; `open`, `stalled`; `resolved`, `rejected`, `deleted`. Other statuses are placed by name and reported. |
| `PriorityAsString`, `EnablePriorityAsString` | Priority names, per queue | RT 5.0 and 6.0: RT's default names, Low (0), Medium (50), High (100). RT 4.4: the same three buckets. |
| `Timezone` | The day of a due date, a start date, and of time worked | UTC, or `--timezone`. |

RT 4.4 keeps every setting in `RT_SiteConfig.pm`, which a database export cannot read. Lifecycles
defined there are not known to the tool; give their statuses' categories with
`--status-category-map`.

### Priority names (5.0 and later)

RT 4.4 shows priorities as numbers; RT 5.0 added names (`$EnablePriorityAsString`, on by default).
The tool uses the names RT shows. For RT 4.4, and for a queue RT shows as numbers, it uses the three
buckets above. The number itself is always kept in the issue's `extra`.

### Who logged time, and on which day (6.0)

RT 6.0 added `Transactions.TimeWorker` and `Transactions.TimeWorkedDate`: time can be logged for
someone else and for another day. The tool uses them for the time entry's user and date. Before 6.0
the time is the transaction creator's, on the day of the transaction (in the time zone above).

### A ticket description of its own (6.0)

RT 6.0 added `Tickets.Description`, an HTML text separate from the messages. When a ticket has one, it
becomes the issue's description and the message the ticket was created with becomes its first
journal entry. Before 6.0, and when it is empty, the description is that first message.

Its changes are `Set` transactions of the field `Description`, carried as description changes.

### Long transaction values (6.0)

RT 6.0 keeps an old or new value longer than 255 bytes (a changed description, say) in the
`ObjectContents` table, as JSON, and marks the transaction with `ReferenceType = 'RT::ObjectContent'`.
The tool reads the values from there.

### Saved searches and dashboards (6.0)

RT 6.0 moved saved searches and dashboards from `Attributes` into `SavedSearches` and `Dashboards`.
Neither is carried; the tool counts them from both places.

### Article type (6.0)

RT 6.0 added `Articles.Type`. The tool does not need it.

### Other changes

These exist and do not change what the tool reads:

| Version | Change |
|---|---|
| 5.0 | `Attachments.id`, `Transactions.id` and their references became 64-bit; `Transactions.Field` grew to 255 characters. |
| 5.0 | `AuthTokens`, `Shorteners` tables; `Users.TOTPSecret`; `CustomFields.ValidationHint`; `CustomRoles.LookupType`. |
| 6.0 | `Users.Image`; a `LookupType` on scrips, scrip actions and conditions, and templates; `Templates.Queue` renamed `ObjectId`. |
| 6.0 | The `HTML` custom field type (the default type of an article's `Content` field). Earlier versions store an article's rich text in a `Text` field. |

## Differences between the databases

RT creates the same tables on every database from its own schema files; the names differ in case
only. PostgreSQL folds them to lower case (`tickets.effectiveid`); MySQL keeps a table's name as
written (`Tickets`) and matches column names in any case. The tool names each table as the database
spells it.

Content is stored differently:

| | PostgreSQL | MySQL and MariaDB |
|---|---|---|
| `Attachments.Content`, `ObjectCustomFieldValues.LargeContent` | `text`: anything that is not valid UTF-8 is base64-encoded (`ContentEncoding = 'base64'`) or quoted-printable | `LONGBLOB`: the bytes as they are (`ContentEncoding = 'none'`) |
| `Configurations.Content`, `Attributes.Content` | `text` | `LONGBLOB` |

The tool reads both, and asks MySQL for `utf8mb4`, the character set RT's MySQL schema uses.

## Values RT writes in every version

- **Times** are UTC, without a time zone. An unset date (`Told`, `Starts`, `Due`, `Resolved`, ...) is
  `1970-01-01 00:00:00`, which the tool reads as "not set".
- **Merged tickets** keep their own row, with `EffectiveId` naming the ticket they were merged into.
- **IP address custom field values** are stored zero-padded (`192.000.002.010`) so they sort; the tool
  writes them as RT shows them (`192.0.2.10`).
- **Mail** is stored as RT received it, one row of `Attachments` per MIME part. RT converts text parts
  to UTF-8 and records the original charset in `X-RT-Original-Encoding`; older data may still be in
  the charset the part declares. The tool tries UTF-8 first, then the declared charset.
