"""The RT fixture keeps every checkout's data to itself, and builds what it says it builds."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.fixture.yml"
STORAGE_TARGET = "/opt/rt/var/external-storage"


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return data


def test_external_storage_is_a_named_volume() -> None:
    compose = _compose()
    for name in ("rt-pg", "rt-mysql"):
        mounts = [volume.split(":") for volume in compose["services"][name]["volumes"]]
        sources = [source for source, target, *_ in mounts if target == STORAGE_TARGET]
        assert len(sources) == 1, name
        assert not sources[0].startswith((".", "/", "~", "$")), name
        assert sources[0] in compose["volumes"]


def test_project_name_is_not_fixed() -> None:
    assert "name" not in _compose()
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"^FIXTURE_PROJECT \?= (.+)$", makefile, re.MULTILINE)
    assert match is not None and "CURDIR" in match.group(1)
    assert "-p $(FIXTURE_PROJECT)" in makefile


def test_databases_keep_no_state_on_disk() -> None:
    services = _compose()["services"]
    assert "/var/lib/postgresql/data" in services["rt-db-pg"]["tmpfs"]
    assert "/var/lib/mysql" in services["rt-db-mysql"]["tmpfs"]


def test_ports_default_to_the_documented_ones() -> None:
    services = _compose()["services"]
    assert services["rt-db-pg"]["ports"] == ["127.0.0.1:${RT_TOOL_FIXTURE_PG_PORT:-5462}:5432"]
    assert services["rt-db-mysql"]["ports"] == ["127.0.0.1:${RT_TOOL_FIXTURE_MYSQL_PORT:-3319}:3306"]
    readme = (ROOT / "README.md").read_text()
    assert "5462" in readme and "3319" in readme


def test_every_release_is_pinned() -> None:
    """Each RT release names the commit of its tag and a base image by digest."""
    makefile = (ROOT / "Makefile").read_text()
    for version in ("4.4.9", "5.0.11", "6.0.4"):
        commit = re.search(rf"^RT_COMMIT_{re.escape(version)} := ([0-9a-f]+)$", makefile, re.MULTILINE)
        assert commit is not None and len(commit.group(1)) == 40, version
        assert re.search(rf"^RT_BASE_{re.escape(version)} := \$\(RT_BASE_\w+\)$", makefile, re.MULTILINE), version
    for base in re.findall(r"^RT_BASE_(?:OLD|NEW) := (.+)$", makefile, re.MULTILINE):
        assert re.search(r"@sha256:[0-9a-f]{64}$", base), base
    dockerfile = (ROOT / "fixture" / "Dockerfile").read_text()
    assert 'test "$(git -C /usr/src/rt rev-parse HEAD)" = "${RT_COMMIT}"' in dockerfile
