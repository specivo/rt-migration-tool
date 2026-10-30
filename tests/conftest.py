"""Shared helpers: a sample RT to export, and a way to read the bundle back."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests import sample_rt


@dataclass
class Bundle:
    """A bundle read back from a directory: manifest, lookups and every record file."""

    root: Path
    manifest: dict[str, Any]
    lookups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def get(self, stem: str) -> list[dict[str, Any]]:
        return self.records.get(stem, [])

    def by_ref(self, stem: str) -> dict[str, dict[str, Any]]:
        return {record["source_ref"]: record for record in self.get(stem)}

    def not_carried(self) -> dict[str, int]:
        return {item["what"]: item.get("count", 0) for item in self.manifest.get("not_carried_over", [])}


def read_bundle(root: Path) -> Bundle:
    bundle = Bundle(root=root, manifest=json.loads((root / "manifest.json").read_text()))
    if (root / "lookups.json").exists():
        bundle.lookups = json.loads((root / "lookups.json").read_text())
    for path in sorted(root.glob("*.jsonl")):
        bundle.records[path.stem] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return bundle


@pytest.fixture(params=["6.0"])
def sample(request: pytest.FixtureRequest, tmp_path: Path) -> sample_rt.SampleRT:
    return sample_rt.build(tmp_path / "source", version=request.param)


Export = Callable[..., Bundle]


@pytest.fixture
def export(sample: sample_rt.SampleRT, tmp_path: Path) -> Export:
    """Run the command over the sample and return the bundle it wrote."""
    from specivo_rt_migration.cli import main

    counter = iter(range(1000))

    def run(*extra: str, storage: bool = True, source: sample_rt.SampleRT | None = None) -> Bundle:
        rt = source or sample
        out = tmp_path / f"bundle-{next(counter)}"
        argv = ["--db-url", rt.url, "--instance", "rt.example.com", "--out", str(out), "--quiet", *extra]
        if storage:
            argv += ["--external-storage-path", str(rt.storage)]
        assert main(argv) == 0
        return read_bundle(out)

    return run
