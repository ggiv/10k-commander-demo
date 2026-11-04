from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from growth_cost.contracts import json_bytes
from growth_cost.pipeline import build
from growth_cost.sec import Archive, read_bundle, write_bundle
from growth_cost.warehouse import publish

FIXTURES = Path(__file__).parent / "fixtures"
CUTOFF = date(2026, 11, 4)


@pytest.fixture(scope="session")
def example(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    root = tmp_path_factory.mktemp("example")
    manifest = build(
        root,
        FIXTURES / "bundle/manifest.json",
        FIXTURES / "companies.csv",
        FIXTURES / "concepts.yml",
        CUTOFF,
    )
    publish(root, manifest["release_id"])
    return root, manifest


@pytest.fixture
def mutate_bundle(tmp_path: Path) -> Callable[..., Path]:
    def mutate(
        cik: str,
        change: Callable[[dict[str, Any]], None],
        *,
        synthetic: bool = True,
        submissions: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        source, snapshots, _ = read_bundle(FIXTURES / "bundle/manifest.json")
        destination = Archive(tmp_path / "bundle")
        captures = []
        for snapshot in snapshots:
            payload = source.payload(snapshot)
            if snapshot.cik == cik and snapshot.kind == "companyfacts":
                change(payload)
            if snapshot.cik == cik and snapshot.kind == "submissions" and submissions:
                submissions(payload)
            captures.append(
                destination.capture(
                    json_bytes(payload),
                    url=snapshot.url,
                    kind=snapshot.kind,
                    cik=snapshot.cik,
                    run_id="mutated-test",
                )
            )
        path = destination.root / "manifest.json"
        write_bundle(path, captures, synthetic=synthetic)
        return path

    return mutate
