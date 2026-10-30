---
description: Install the specivo-rt-migration command from a GitHub release with uv or pipx, or from a source checkout, and check that it runs.
---

# Installation

The tool is a Python command, `specivo-rt-migration`. It needs **Python 3.12 or later**. There is no
package on PyPI: install it from a tagged release on GitHub. The database drivers for PostgreSQL
(`asyncpg`) and MySQL or MariaDB (`aiomysql`) come with it.

## From a release

Pick the release you want from the
[releases page](https://github.com/specivo/rt-migration-tool/releases) and install it with
[uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/):

```bash
uv tool install git+https://github.com/specivo/rt-migration-tool@vX.Y.Z
# or
pipx install git+https://github.com/specivo/rt-migration-tool@vX.Y.Z
```

Replace `vX.Y.Z` with the release tag. Both commands put `specivo-rt-migration` on your path in an
isolated environment.

## From a source checkout

```bash
git clone https://github.com/specivo/rt-migration-tool.git
cd rt-migration-tool
uv sync
uv run specivo-rt-migration --version
```

## Check that it runs

```bash
specivo-rt-migration --version
specivo-rt-migration --help
```

## Where to run it

Run the tool on a machine that can reach RT's database: the RT server itself, a database replica,
or any machine allowed through to the database port. If RT keeps attachments in Disk external
storage, the machine also needs to read that directory (or a copy of it).

The tool reads everything in one long read-only transaction. On a large installation, run it against
a replica, or at a quiet hour: a long-open transaction keeps the database from cleaning up old row
versions while it runs.
