---
description: What specivo-rt-migration does not carry from Request Tracker to Specivo, and how it reports each gap through the not_carried_over list in the bundle manifest and the extra data on records.
---

# What is not carried over

Nothing RT holds should disappear silently. The tool records what it left out in two places:

- **`not_carried_over`** in the bundle's `manifest.json`: one entry for each kind of thing, with a
  description and a count. Specivo prints these entries in its import report.
- **`extra`** on a record: RT data that has no place in Specivo, kept with the record it belongs to,
  for example a ticket's RT number, its reminders, its requestors, or the mail headers of a reply.
  Specivo does not import `extra`, but keeps it with the record's import data.

When the tool finishes, it prints how many kinds of data are listed in the manifest. To read them,
open `manifest.json` and look at `not_carried_over`; each entry has a `what` and a `count`.

## What is counted

| Left out | Why, and where to find it |
|---|---|
| Deleted tickets | Tickets in the `deleted` status are not carried. |
| Reminders | Specivo has no reminders. Each ticket's reminders are kept in its `extra` (`reminders`), and reminder changes in the history are counted. |
| Approval tickets | RT's approvals workflow has no equivalent. |
| Merged tickets | Not issues of their own: folded into the ticket they were merged into (see [Merged tickets](carried-over.md#merged-tickets)). Counted so the numbers add up. |
| Merged tickets of queues not exported, deleted merged tickets | Left out of the ticket they were merged into, so one queue's text does not reach another queue's project. |
| Merged tickets whose surviving ticket is not in the bundle | The surviving ticket is of a queue not exported, deleted, a reminder or approval, or cut by `--limit`. |
| Field changes of merged tickets | A merged ticket's own status, owner, watcher, subject and custom field changes are not replayed on the surviving issue; its messages, files, time and links are. |
| Copies of mail RT sent (`EmailRecord`, `CommentEmailRecord`) | RT keeps a copy of each notification it sent. The reply or comment itself is carried. |
| Forwarded messages and tickets, system errors, other transaction types | Counted by kind. |
| Files of private comments | A Specivo attachment has no private flag, so a comment's files (and the images it shows inline) would be visible to everyone who can see the issue. `--include-comment-files` carries them anyway. |
| Negative time corrections | Time worked reduced in RT. Specivo time entries record time spent. |
| Links to things other than tickets | URLs, articles, assets. Kept in the issue's `extra` (`links`). |
| Links to tickets of other RT servers | A ticket URI whose host is not this RT's organization. Kept in the issue's `extra` (`links`). |
| Links to deleted tickets, reminders, approvals, or tickets RT no longer has | Counted. |
| A second parent of a ticket | Specivo has one parent per issue; the others are kept as *relates*. |
| A relation between a ticket and its parent or child | Specivo refuses it; the parent is kept. |
| History of custom fields that are not carried | Image and Binary fields (their values are files), and fields of queues not exported. |
| Custom fields of queues, users, groups, transactions and assets, and their values | Specivo has custom fields on issues only. |
| Files in RT's external storage that were not read | Without `--external-storage-path`, or in a backend other than Disk (Amazon S3, Dropbox). |
| Files missing from external storage, or that could not be decoded | Counted with the reason. |
| Rights (RT's ACL) | Specivo creates roles without permissions; memberships carry who has which role (see [Memberships](carried-over.md#memberships-and-roles)). |
| Rights granted to RT's Everyone or Unprivileged groups, or to per-ticket roles | They name no one in particular, so they make no membership. What Everyone holds is also named, queue by queue (see [RT's Everyone group](#rts-everyone-group)). |
| Rights granted to RT's Privileged group that amount to no role | Rights such as `SeeQueue` or `ShowApprovalsTab`. Those that do amount to a role make the group *RT privileged users* (see [RT's Privileged group](carried-over.md#rts-privileged-group)). |
| Rights on other objects | Rights on custom fields, classes, catalogs and groups. |
| Settings in RT's database that could not be read | A `Configurations` value that is neither plain `Data::Dumper` data nor valid JSON. A warning names the setting, and RT's default is used (its default lifecycle, priorities and time zone). |
| Old and new values RT 6.0 kept apart that could not be read | The change is carried with the values the transaction itself holds. |
| Passwords | Specivo cannot use RT's password hashes. Every account sets a new password. |
| Disabled articles | Left out. |
| Articles of classes outside the exported queues | With `--queue`, only the classes of those queues are carried. |
| Article topics | No equivalent. |
| Assets and asset catalogs | Specivo has no asset tracking. |
| Scrips and templates | RT's automation. Specivo has its own. |
| Saved searches and dashboards | Counted from `Attributes` and, on RT 6.0, their own tables. |
| Queues with service levels (SLA) | Specivo has no SLAs; a ticket's SLA is kept in its `extra`. |

## RT's Everyone group

Rights RT grants to its *Everyone* group are never carried, and no project is made public because of
them. *Everyone* is not just the staff: it takes in every requestor, and anyone whose mail RT turned
into a ticket gets an account in it. `ShowTicket` for Everyone on a queue lets any requestor read
every ticket of that queue, not only their own; a public Specivo project would show its issues to
every account on the instance.

So an administrator can decide, the tool names what Everyone holds:

- in `not_carried_over`, one entry for the rights granted on every queue (globally), for example
  `rights granted to RT's Everyone group on every queue: CreateTicket, ReplyToTicket`, and one entry
  for each exported queue with rights of its own, for example
  `rights granted to RT's Everyone group on queue Support: ReplyToTicket, ShowTicket`, with the
  project's `source_ref` (`entity_type` `project`);
- in each project's `extra`, `rt_everyone_rights`: everything Everyone holds on that queue, the
  global rights included.

The count `rights granted to everyone in RT's Everyone group` stays, as for RT's other groups. To
make a project public after the import, see [Loading](loading.md#rts-everyone-group-and-public-projects).

## What is not read at all

- **Shredded data.** RT's shredder deletes rows for good. What it left behind (a link or a merge
  naming a ticket that no longer exists) is counted as above; what it removed cannot be.
- **RT's configuration files.** Settings in `RT_SiteConfig.pm` (and, on RT 4.4, every setting) cannot
  be read from the database. Lifecycles and priority names defined there fall back to RT's defaults
  (see [RT versions](versions.md#settings-kept-in-the-database-50-and-later)).
- **Ticket and transaction custom fields' display rules**, custom field groupings, dashboards'
  contents, user preferences and bookmarks.
- **Encryption.** Messages RT stored encrypted are carried as stored.
