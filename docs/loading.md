---
description: Check, rehearse and load a bundle written by specivo-rt-migration into Specivo with the core import command, including --files-path, --continue-import and reading the import report.
---

# Validating and loading the bundle

Loading is done by Specivo, not by this tool. It needs **Specivo 0.11.0 or later**. The import command
is the same for every migration tool; this page shows what matters for an RT bundle. The full guide is
[Importing from another tracker](https://specivo.io/docs/specivo/install/importing/) in the Specivo user
guide.

!!! warning "Back up first"
    Back up your Specivo database and data directory before a real import.

Copy the bundle to the Specivo server, in a folder that is writable by the app (an import keeps a
small links file next to the bundle). Then run, in this order:

```bash
# 1. Check the bundle. Reads only the bundle: no database is touched.
python -m specivo.cli.import_data --source bundle --path rt-export --validate

# 2. Rehearse. The whole import runs in a transaction that is always rolled back.
python -m specivo.cli.import_data --source bundle --path rt-export --dry-run

# 3. Import.
python -m specivo.cli.import_data --source bundle --path rt-export
```

With Docker, prefix each command with `docker compose exec api` and use the path inside the container.

## Attachments kept apart

A bundle written with `--files-path` has no `files/` folder. Copy the files folder along and name it
on every command:

```bash
rsync -a --partial rt-export rt-files specivo-host:/srv/import/
python -m specivo.cli.import_data --source bundle --path /srv/import/rt-export \
    --files-path /srv/import/rt-files --validate
```

## Several bundles of one RT

Bundles of one RT installation (one per queue, or a later export before the switch) share the
`--instance` they were written with. Load the first as above, and every later one with
`--continue-import`:

```bash
python -m specivo.cli.import_data --source bundle --path rt-billing --continue-import
```

Records already imported are skipped; links and references between the bundles are made once both
ends are in Specivo, whichever bundle came first. Keep the links file Specivo writes next to the
bundles (`specivo-links.rt.<instance>.sqlite`) and give every load the same one.

## Options worth knowing for RT

| Specivo option | When |
|---|---|
| `--import-admins` | Make RT's superusers Specivo administrators. Without it they are imported as ordinary accounts and listed in the report. |
| `--merge-duplicate-statuses` | Specivo already has statuses named like RT's (`new`, `open`, `resolved`): use those instead of adding a second one of each name. |
| `--project-key-map support=SUP` | Choose a project's key instead of the one derived from the queue's identifier. |
| `--project <refs>` | Load only some projects of the bundle (by the queue's RT id). |

## Reading the report

The import report lists what it created, what it skipped, and the bundle's `not_carried_over`: what
RT had that the bundle could not hold (see [What is not carried over](not-carried-over.md)). It also
lists every user who is not active (RT's unprivileged and disabled users are locked), every account
RT marked as an administrator, and every account without an email address.

After the import:

- give the roles **Manager**, **Developer** and **Reporter** their permissions: Specivo creates them
  without any, and each role's import data names the RT rights that led to it;
- check the projects of article classes (*Articles: ...*): a class applied to several queues has a
  project of its own;
- requestors who should sign in (a customer portal) need their accounts unlocked, or an export with
  `--activate-unprivileged`.
- check the group **RT privileged users**, if the bundle has one: it stands for RT's *Privileged*
  group, with every privileged user carried as a member;
- check what RT's *Everyone* group could do (below).

## RT's Everyone group and public projects

Every imported project is **private**: only its members (directly or through a group) can open it.
The report's `not_carried_over` names the rights RT's *Everyone* group held, on every queue and queue
by queue, and each project keeps its list in `extra` (`rt_everyone_rights`). Everyone in RT includes
requestors and anyone who mailed a ticket in, so a right such as `ShowTicket` there seldom means the
queue was meant to be open to all (see [What is not carried over](not-carried-over.md#rts-everyone-group)).

If a project really should be readable by everyone with an account, make it public after the
import. A public project shows its content to every signed-in user who is not a member, with the
permissions of the built-in **Non member** role: by default it may view issues, and an
administrator changes that under **Administration → Roles**. Specivo's web pages show a project's
visibility but have no switch for it; an administrator, or a user whose role in the project has the
`manage_project` permission, changes it through the REST API:

```bash
curl -X PATCH https://your-specivo-host/api/v1/projects/SUPPORT/ \
  -H "Authorization: Bearer spv_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"is_public": true}'
```

`{"is_public": false}` makes it private again. Reading without an account is separate: an instance
administrator opts a public project in under its **Settings** and turns on anonymous access for the
whole instance.
