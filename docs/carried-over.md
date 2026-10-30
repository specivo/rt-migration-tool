---
description: What specivo-rt-migration carries from Request Tracker to Specivo, and how each RT record and field maps to the import bundle - queues, tickets, history, files, time, links, people, rights, custom fields and articles.
---

# What is carried over

Every decision below is the tool's default. Where RT has something the bundle has no place for, the
data is kept in the record's `extra` or counted in the manifest's `not_carried_over` (see
[What is not carried over](not-carried-over.md)). Record identifiers (`source_ref`) are RT's own ids,
so exporting the same RT again gives every record the same identifier, and a later import adds only
what is new.

## Queues

Each queue becomes a **project**, with the modules issue tracking, time tracking and wiki.

| RT | Specivo |
|---|---|
| Name | Name. The identifier is the name in lower case, letters, digits and `-` (`Old Projects` becomes `old-projects`); a name in another script gives `queue-<id>`. Identifiers that come out the same (`IT Help` and `IT-Help`) are made unique by adding `-2`, `-3`, ... in queue id order; the projects of article classes (`kb-<class name>`) come after the queues. Specivo derives the project key from it. |
| Description | Description |
| Disabled | Archived (status 9): read-only and out of the way, as a disabled queue is in RT |
| Lifecycle, correspond and comment addresses, subject tag | `extra` |
| Rights of RT's *Everyone* group on the queue, or on every queue | `extra` (`rt_everyone_rights`). The project stays private (see [What is not carried over](not-carried-over.md#rts-everyone-group)). |

RT's internal `___Approvals` queue is not exported. `--queue NAME` (repeatable) exports only the
queues named, by name or id.

## Tickets

Each ticket becomes an **issue** of its queue's project, in ticket number order. Specivo numbers
issues itself (`SUPPORT-1`, `SUPPORT-2`, ...), so RT's number is kept in the issue's `extra`
(`rt_ticket`), and references to it in text become links (see [References](#references-to-other-tickets)).

| RT | Specivo |
|---|---|
| Subject | Subject |
| Creator | Author |
| Owner | Assignee. `Nobody` is no one. |
| Status | Status (see [Statuses](#statuses)) |
| Priority | Priority (see [Priorities](#priorities)); the number in `extra` |
| Due, Starts | Due date, start date: the day in RT's time zone (or `--timezone`, or UTC) |
| Created, LastUpdated | Created, updated |
| Resolved | Closed at, when the ticket is in an inactive status |
| TimeEstimated | Estimated hours |
| TimeLeft | Remaining estimate (hours) |
| TimeWorked | Time entries (see [Time](#time)); the total in `extra` |
| Told, Started, InitialPriority, FinalPriority, Type, SLA | `extra` |
| Requestors | Watchers, and their names in `extra` (`requestors`) |
| Cc, AdminCc, custom roles | Watchers (a group in a role gives its users), and every role's members in `extra` (`roles`) |
| Description (RT 6.0) | Description |

**The description** is the first message: the message of the transaction that created the ticket.
RT 6.0 tickets may have a description of their own; then that is the description, and the first
message becomes the first journal entry.

**Left out**: tickets in the `deleted` status, reminders and approvals, each counted. A ticket's
reminders are kept in its `extra` (`reminders`: subject, status, owner, due), since Specivo has no
place for them. `--limit N` exports at most N tickets of each queue, for a trial run.

### Merged tickets

A merged ticket (RT keeps it, with `EffectiveId` naming the ticket it was merged into) is not an
issue of its own. Its messages are folded into the surviving ticket: its first message, replies,
comments, files, time worked and link changes become journal entries, files and time entries of the
surviving issue, marked `merged_from` (the merged ticket's number) in their `extra`. Its own field
changes (status, owner, watchers, subject, custom fields and the like) are not replayed, since they
changed the merged ticket and not the surviving one; they are counted. The issue's `extra` lists the
merged ticket numbers (`merged_tickets`).

A merged ticket is folded only when it would be exported itself: its own queue is exported and it is
not deleted. A merged ticket of another queue is left out of the surviving issue, so text a queue
showed to its own people does not reach another queue's project; it is counted. So is a merged ticket
whose surviving ticket is not in the bundle (of a queue not exported, deleted, a reminder or approval,
or cut by `--limit`).

A reference to a merged ticket's number, and a link to it, lead to the surviving ticket, as they do in
RT: to the surviving issue, to its issue in another bundle, or nowhere (the text stays) when the
surviving ticket is deleted, a reminder or an approval.

### Statuses

RT's statuses become Specivo statuses, by name. Their category comes from the lifecycle that lists
them: **initial** is backlog, **active** is active, **inactive** is closed. RT 5.0 and 6.0 keep
lifecycles changed in the web interface in the database, and the tool reads them; otherwise it knows
RT's default lifecycle. A status no known lifecycle lists (one of a lifecycle defined in RT's
configuration file) is placed by its name, reported when the export ends, and can be placed with
`--status-category-map STATUS=CATEGORY,...` (categories: backlog, active, done, closed).

The `deleted` status is not created: deleted tickets are left out.

### Priorities

RT priorities are numbers. RT 5.0 and 6.0 show them by name (`%PriorityAsString`, per queue: by
default Low from 0, Medium from 50, High from 100), and the tool uses the name RT shows for each
ticket. RT 4.4 shows numbers, and so do queues RT 5.0 is told to show as numbers: those are put in
the same three buckets, and the bucket is named in `extra` (`priority_bucket`). Every name any queue
uses is one Specivo priority, lowest first; the number is always kept in `extra`.

## History

Each transaction of a ticket becomes a **journal entry**, with its author and time. Transactions one
person made in the same second (taking a ticket, opening it and replying) are one entry, as they were
one action in RT. A comment is the exception: it is always an entry of its own, private as a whole,
so a change or a reply made in the same second never becomes private with it, and the comment never
becomes public with them. Every entry keeps its RT transaction ids in `extra`.

| RT transaction | Journal entry |
|---|---|
| Correspond (reply) | A public note: the message's text, its files |
| Comment | A private note (its files are left out by default, see [Files](#files)) |
| Status | Status change, by status |
| Give, Take, Steal, Untake, Set Owner | Assignee change, by user |
| Set Queue | Project change, by project |
| Set Priority | Priority change, by priority (a change within one name is kept as the numbers, `priority`) |
| Set Subject, Due, Starts, TimeEstimated | Subject, due date, start date, estimated hours |
| Set Description (RT 6.0) | Description change, in Markdown |
| Set of another field (Type, InitialPriority, FinalPriority, SLA) | A change named after the field, as RT wrote it |
| AddWatcher, DelWatcher, SetWatcher | A change of `requestor`, `cc`, `admincc` or a custom role, naming the person or group |
| AddLink, DeleteLink | A relation change (`blocked_by`, `blocks`, `relates`, `has_child`), naming `#number`; a parent (`MemberOf`) as a parent change |
| Merge (`MergedInto`) | A `merged` relation change naming the merged ticket |
| CustomField | A custom field change, with the old and new value |
| Set TimeWorked, TimeLeft; Told | Nothing: time worked is carried as time entries, and Specivo keeps no history of the remaining estimate |

Everything else (copies of mail RT sent, reminder changes, forwards, system errors) is counted, not
carried.

### Messages

A reply's or comment's text comes from its message as RT stored it, one MIME part per row:

- the text a reader sees is the first text part with no file name that is not marked as an
  attachment; later such parts (a list footer, say) are added to it;
- of a `multipart/alternative`, the HTML part is used, which keeps the formatting, else the plain
  text;
- every other part with content becomes a **file** of the issue, with the transaction's author and
  time; a part without a name is named `part-<id>.<ext>`. The parts of a forwarded message are files.
  An image the HTML shows by its `Content-ID` (`cid:`) is a file too, shown in place by an image
  placeholder.

**HTML** is cleaned to the tags and attributes Specivo renders (scripts, styles and event handlers are
dropped), the way the Redmine tool does it, and converted to Markdown. An image placeholder is written
only for a file of the message itself; nothing a message contains can make it name another file. **Plain text** is kept as it
reads: characters Markdown would read as formatting are escaped, and line breaks are kept.

The **Message-ID, sender and recipients** (From, To, Cc, Bcc, and the `RT-Send-CC` and `RT-Send-BCC`
of a message sent from RT) and the subject go into the entry's `extra`, not into the text.

Text is decoded by the charset its part declares; RT stores mail converted to UTF-8, so UTF-8 is
tried first. Content RT encoded (base64, quoted-printable) is decoded. Content RT moved to its
**external storage** is read from the Disk backend when given its root with
`--external-storage-path`; see [Exporting](exporting.md#attachments-in-external-storage).

### References to other tickets

Text that names a ticket in one of these ways becomes an issue placeholder, which Specivo turns into
the issue's new key:

- RT's subject tag, `[rt.example.com #123]`;
- `RT #123`, `RT#123`, `RT 123` (`RT` in capitals);
- `ticket #123`, `ticket 123`, `Ticket #123`, `ticket#123`.

A bare `#123` is linked only with `--link-hash-numbers`: in mail, `#2` is as often a list item or a
colour as a ticket. Text in code, in links and in URLs is left alone, and so is a number RT has no
ticket for. A ticket of another queue exported in a bundle of its own becomes a placeholder that
keeps the text it had (`{{issue:123|RT #123}}`): once both bundles are loaded it is a link, and until
then it reads as before. References to deleted tickets, reminders and approvals stay text.

## Files

Every file of a message, and every value of an Image or Binary custom field, becomes an
**attachment** of the issue. Content stored twice is stored once in the bundle. An attachment keeps
its RT transaction (and `Content-ID`) in `extra`; a custom field file says which field it was.

**Files of comments** are left out by default, and counted. A comment is a private note, but a
Specivo attachment has no private flag: it is visible to everyone who can see the issue. An image a
comment shows inline is left out too, and the note shows no image. `--include-comment-files` carries
them anyway, as files of the issue that everyone who can see the issue can see.

## Time

Each transaction that logged time (`TimeTaken`) becomes a **time entry** on the issue's project, of
the activity *Work*, the transaction that created the ticket included (RT records the time worked
given on creation there), with the hours to two decimals and the minutes in `extra`. RT 6.0 records who
did the work and on which day (`TimeWorker`, `TimeWorkedDate`), and the entry uses them; earlier
versions record neither, and the entry is the transaction creator's, on the day of the transaction.
A negative correction (time worked reduced in RT) is counted, not carried: Specivo time entries are
time spent.

## Links

| RT link | Specivo |
|---|---|
| MemberOf | The child issue's parent (a second parent is kept as *relates*, and counted) |
| DependsOn | *blocks*: the ticket depended on blocks the other |
| RefersTo | *relates*, once per pair |
| MergedInto | See [Merged tickets](#merged-tickets) |

RT lets two tickets be parent and child and also refer to each other; Specivo refuses a relation
between an issue and its parent (or any ancestor), so such a relation is left out and counted: the
parent says it already.

A link to a ticket of another queue that is exported in a bundle of its own is written as a
cross-bundle reference: Specivo makes it once both bundles are loaded. A link to something that is
not a ticket (a URL, an article, an asset) is kept in the issue's `extra` (`links`) and counted. Links
to deleted tickets, reminders and approvals are counted.

RT names tickets by URI, `fsck.com-rt://<Organization>/ticket/<number>`, and can link to tickets of
another RT. A link is between two tickets of this RT when RT stored it so (`LocalBase`,
`LocalTarget`), or when the URI's host is this RT's `$Organization`. The tool reads `$Organization`
from RT's settings in the database when it is there, else takes the host of the URIs RT stored as
local. A link to a ticket of another RT is kept in the issue's `extra` (`links`) and counted, and its
history shows the URI. If the tool cannot tell the organization (no setting, no local link), only the
links RT stored as local are links between tickets.

## People

Every **user** RT has except `RT_System` and `Nobody`:

| RT | Specivo |
|---|---|
| Name | Login |
| RealName (or Name) | Display name |
| EmailAddress | Email |
| Lang | Language |
| Privileged and enabled | Active |
| Unprivileged (requestors, mail senders) | Locked, so authorship is kept but they cannot sign in; `--activate-unprivileged` makes them active |
| Disabled | Locked |
| SuperUser right (directly or through a group) | Administrator (Specivo honours it only with its `--import-admins`) |
| Organization, NickName, Timezone | `extra` |

Passwords are not carried: Specivo cannot use RT's hashes, and every account sets a new password.

**User-defined groups** become groups, each with every user in it, directly or through groups in it.
RT's *Privileged* group becomes the group **RT privileged users** when it gets a membership (see
[Memberships](#rts-privileged-group)). RT's other internal groups (*Everyone*, *Unprivileged*) and
role groups are not groups in Specivo.

**An export of selected queues** (`--queue`) carries only the people its content names: the creators,
owners, requestors, Cc, AdminCc and custom role members of its tickets (and of the tickets merged into
them and their reminders), the authors of their history and the people who worked time on them, the
people named in their owner and watcher changes, the authors of its articles, and the users and
members of groups that get a membership in one of its projects. Only the groups that get a
membership in one of its projects are carried. A full export carries every user and group.
RT privileged users holds the privileged users the export carries, not every privileged user of RT.

## Memberships and roles

RT grants rights on a queue, or globally on every queue, to users, groups and roles. The tool works
out the rights each user and user-defined group holds on each exported queue, and gives it the
Specivo role those rights amount to:

| Rights held | Role |
|---|---|
| `AdminQueue` | Manager |
| `OwnTicket` or `ModifyTicket` | Developer |
| `ShowTicket`, and none of the above | Reporter |

Rights granted to a queue's **AdminCc** or **Cc** role reach that queue's AdminCc and Cc watchers.
Rights granted to the per-ticket roles (Owner, Requestor, a ticket's Cc and AdminCc) and to RT's
*Everyone* and *Unprivileged* groups name no one in particular, so they make no membership; they are
counted. A group's members hold its role through the group.

### RT's Privileged group

RT's *Privileged* group holds every privileged user: the staff, as opposed to requestors. Rights
granted to it on a queue, or globally, become one group, **RT privileged users** (`source_ref`
`rt-privileged`, RT's group id in `extra`), whose members are the privileged users the export
carries, disabled ones included. On each exported queue where it holds rights, the group gets a
membership with the role those rights amount to, by the table above: `ShowTicket` alone on a queue
makes it a Reporter there, `OwnTicket` a Developer, `AdminQueue` a Manager. Global rights count on
every queue.

The group is written only when it gets at least one membership. Rights of the Privileged group that
amount to no role (`SeeQueue`, `CreateTicket`, `ShowApprovalsTab`, ...) are counted, as before. A
user who also has a membership of their own, or through a user-defined group, holds every one of
those roles in the project.

Specivo creates the three roles without permissions, for an administrator to fill in; each role's
`extra` names the RT rights that lead to it. RT's rights themselves are not carried.

## Custom fields

Custom fields of tickets become issue custom fields, applied to the projects of the queues RT applies
them to, or to every project when RT applies one globally. Specivo shows each by its RT name; its
metadata key comes from the name (letters, digits and `_`, or `cf_<id>` for a name in another
script), and `--cf-key-map NAME=KEY` overrides it.

| RT type | Specivo |
|---|---|
| Select | `list`, its values (and any value a ticket still holds) as the choices |
| Freeform, Combobox, Autocomplete | `string`; suggested values in `extra` |
| Text, Wikitext | `text`, as written |
| HTML (RT 6.0) | `text`, converted to Markdown |
| Date | `date` (`YYYY-MM-DD`) |
| DateTime | `string`, an ISO 8601 time in UTC |
| IPAddress, IPAddressRange | `string`, as RT shows it (`192.0.2.10`, `198.51.100.0-198.51.100.255`) |
| Image, Binary | No field: each value is a file of the issue |

A field taking more than one value is `multiple`. Each change of a value is a custom field change in
the history. Custom fields of queues, users, groups, transactions and assets are counted, with their
values.

## Articles

RT's articles become **wiki pages**, with every revision:

- **Where.** An article belongs to a class. A class RT applies to exactly one exported queue puts its
  articles in that queue's project wiki. Any other class (applied globally, to several queues or to
  none) becomes a project of its own holding only a wiki, *Articles: <class name>*. An export of
  selected queues (`--queue`) carries only the classes of those queues.
- **Text.** The values of the article's custom fields (by default its one field, `Content`). HTML is
  converted to Markdown. With several fields, each is a section headed by its name. The summary is
  kept in the page's `extra`.
- **Revisions.** RT records every change of a field value. The tool works back from the current
  values to the text the article was created with, then forward through each change; changes one
  person made in the same second are one revision.
- **Titles** are the article names, made unique within a wiki by adding the article number.

Disabled articles are counted, not carried, and so are topics and links between articles and
tickets.
