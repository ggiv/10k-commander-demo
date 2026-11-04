"""The non-synthetic path: gate success, candidate inspection, and publication safety."""

import csv
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from growth_cost.analysis import inspect_metric, report
from growth_cost.contracts import json_bytes
from growth_cost.pipeline import build
from growth_cost.sec import Archive, read_bundle, write_bundle
from growth_cost.warehouse import ValidationError, current_release, publish

FIXTURES = Path("tests/fixtures")
CUTOFF = date(2026, 11, 4)
SOURCE_CIK = "9000000001"
CIKS = (SOURCE_CIK, "9000000011", "9000000012")
RECONCILIATION_FIELDS = (
    "cik",
    "period_start",
    "period_end",
    "canonical",
    "value",
    "unit",
    "source_url",
    "statement_reference",
    "reviewer",
    "reviewed_on",
    "scope_notes",
)


def clone_clean_company(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Three copies of the one fully covered fixture company, marked non-synthetic."""
    source, snapshots, _ = read_bundle(FIXTURES / "bundle/manifest.json")
    archive = Archive(tmp_path / "bundle")
    captures = []
    for snapshot in (s for s in snapshots if s.cik == SOURCE_CIK):
        text = json_bytes(source.payload(snapshot)).decode()
        for cik in CIKS:
            captures.append(
                archive.capture(
                    text.replace(SOURCE_CIK, cik).encode(),
                    url=f"https://example.invalid/sec/{cik}/{snapshot.kind}.json",
                    kind=snapshot.kind,
                    cik=cik,
                    run_id="live-path-test",
                )
            )
    bundle = archive.root / "manifest.json"
    write_bundle(bundle, captures, synthetic=False)
    companies = tmp_path / "companies.csv"
    companies.write_text(
        "cik,ticker,name,business_model,included,rationale\n"
        + "".join(f"{cik},T{cik[-2:]},Clone,subscription,true,test\n" for cik in CIKS)
    )
    config = yaml.safe_load((FIXTURES / "concepts.yml").read_text())
    config["annotations"] = []
    mappings = tmp_path / "concepts.yml"
    mappings.write_text(yaml.safe_dump(config))
    return bundle, companies, mappings


def write_reconciliation(path: Path, needed: list[dict[str, Any]], *, tamper: bool = False) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, RECONCILIATION_FIELDS)
        writer.writeheader()
        for index, row in enumerate(needed):
            value = row["value"] + 1 if tamper and index == 0 else row["value"]
            writer.writerow(
                {
                    **{key: row[key] for key in ("cik", "period_start", "period_end")},
                    "canonical": row["canonical"],
                    "value": value,
                    "unit": row["unit"],
                    "source_url": row["source_url"],
                    "statement_reference": "Synthetic statement line",
                    "reviewer": "test",
                    "reviewed_on": "2026-11-04",
                    "scope_notes": "Synthetic clone",
                }
            )


@pytest.fixture
def live_release(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A reconciled, published, non-synthetic release."""
    root = tmp_path / "warehouse"
    bundle, companies, mappings = clone_clean_company(tmp_path)
    candidate = build(root, bundle, companies, mappings, CUTOFF)
    needed = report(root, "reconciliation_inputs", release_id=candidate["release_id"])["rows"]
    reconciliation = tmp_path / "reconciliation.csv"
    write_reconciliation(reconciliation, needed)
    reconciled = build(root, bundle, companies, mappings, CUTOFF, reconciliation)
    publish(root, reconciled["release_id"])
    return root, reconciled


def test_candidate_can_be_inspected_before_publication(tmp_path: Path) -> None:
    root = tmp_path / "warehouse"
    bundle, companies, mappings = clone_clean_company(tmp_path)
    candidate = build(root, bundle, companies, mappings, CUTOFF)
    trace = inspect_metric(
        root,
        SOURCE_CIK,
        date(2025, 12, 31),
        "revenue_growth",
        release_id=candidate["release_id"],
    )
    assert trace["release_id"] == candidate["release_id"]
    assert trace["inputs"][0]["metric_value"] == pytest.approx(25)
    assert not (root / "current.json").exists()


def test_complete_reconciliation_passes_the_live_gate(
    live_release: tuple[Path, dict[str, Any]],
) -> None:
    root, reconciled = live_release
    assert current_release(root)[1]["release_id"] == reconciled["release_id"]
    assert reconciled["synthetic"] is False
    needed = report(root, "reconciliation_inputs")["rows"]
    assert needed and all(row["reconciled"] for row in needed)


def test_wrong_reconciled_value_fails_the_live_gate(tmp_path: Path) -> None:
    root = tmp_path / "warehouse"
    bundle, companies, mappings = clone_clean_company(tmp_path)
    candidate = build(root, bundle, companies, mappings, CUTOFF)
    needed = report(root, "reconciliation_inputs", release_id=candidate["release_id"])["rows"]
    reconciliation = tmp_path / "reconciliation.csv"
    write_reconciliation(reconciliation, needed, tamper=True)
    tampered = build(root, bundle, companies, mappings, CUTOFF, reconciliation)
    with pytest.raises(ValidationError, match="1 inputs"):
        publish(root, tampered["release_id"])


def unreviewed_candidate(tmp_path: Path, root: Path) -> dict[str, Any]:
    bundle, companies, mappings = clone_clean_company(tmp_path)
    config = yaml.safe_load(mappings.read_text())
    config["reviewed"] = False
    unreviewed = tmp_path / "unreviewed.yml"
    unreviewed.write_text(yaml.safe_dump(config))
    return build(root, bundle, companies, unreviewed, CUTOFF)


def test_unreviewed_release_needs_the_explicit_flag(tmp_path: Path) -> None:
    root = tmp_path / "warehouse"
    candidate = unreviewed_candidate(tmp_path, root)
    with pytest.raises(ValidationError, match="unreviewed"):
        publish(root, candidate["release_id"])
    assert not (root / "current.json").exists()
    publish(root, candidate["release_id"], allow_unreviewed=True)
    published = current_release(root)[1]
    assert published["release_id"] == candidate["release_id"]
    assert published["mapping_reviewed"] is False
    flags = report(root, "reconciliation_inputs")["rows"]
    assert not any(row["reconciled"] for row in flags)


def test_unreviewed_flag_does_not_excuse_missing_reconciliation_when_reviewed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "warehouse"
    bundle, companies, mappings = clone_clean_company(tmp_path)
    candidate = build(root, bundle, companies, mappings, CUTOFF)
    with pytest.raises(ValidationError, match="reconciliation incomplete"):
        publish(root, candidate["release_id"], allow_unreviewed=True)


def test_unreviewed_release_cannot_replace_reviewed_publication(
    live_release: tuple[Path, dict[str, Any]],
    tmp_path: Path,
) -> None:
    root, reconciled = live_release
    other = tmp_path / "other"
    other.mkdir()
    candidate = unreviewed_candidate(other, root)
    with pytest.raises(ValidationError, match="reviewed release"):
        publish(root, candidate["release_id"], allow_unreviewed=True)
    assert current_release(root)[1]["release_id"] == reconciled["release_id"]


def test_synthetic_release_cannot_replace_live_publication(
    live_release: tuple[Path, dict[str, Any]],
) -> None:
    root, reconciled = live_release
    synthetic = build(
        root,
        FIXTURES / "bundle/manifest.json",
        FIXTURES / "companies.csv",
        FIXTURES / "concepts.yml",
        CUTOFF,
    )
    with pytest.raises(ValidationError, match="non-synthetic"):
        publish(root, synthetic["release_id"])
    assert current_release(root)[1]["release_id"] == reconciled["release_id"]


def test_demo_command_leaves_live_publication_in_place(
    live_release: tuple[Path, dict[str, Any]],
) -> None:
    root, reconciled = live_release
    result = subprocess.run(
        [sys.executable, "-m", "growth_cost.cli", "--data-dir", str(root), "demo"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (
        "non-synthetic" in json.loads(result.stderr[result.stderr.index('{\n  "error"') :])["error"]
    )
    assert current_release(root)[1]["release_id"] == reconciled["release_id"]


def test_cloud_command_reports_contract_problems(tmp_path: Path) -> None:
    root = tmp_path / "warehouse"
    build(
        root,
        FIXTURES / "bundle/manifest.json",
        FIXTURES / "companies.csv",
        FIXTURES / "concepts.yml",
        CUTOFF,
    )
    release_id = next((root / "releases").iterdir()).name
    env = {key: value for key, value in os.environ.items() if key != "motherduck_token"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "growth_cost.cli",
            "--data-dir",
            str(root),
            "cloud-publish",
            release_id,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1
    assert "Set motherduck_token" in result.stderr


def test_commands_explain_when_nothing_is_published(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "growth_cost.cli", "--data-dir", str(tmp_path), "status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "No published release" in result.stderr
