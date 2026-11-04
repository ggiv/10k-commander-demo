import json
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from growth_cost.analysis import reproduction_bundle
from growth_cost.contracts import ContractError
from growth_cost.pipeline import build
from growth_cost.warehouse import (
    ValidationError,
    current_release,
    publish,
    release_path,
    rows,
    validate,
    verify_release,
)

FIXTURES = Path("tests/fixtures")
CUTOFF = date(2026, 11, 4)


def build_example(root: Path, bundle: Path | None = None) -> dict[str, Any]:
    return build(
        root,
        bundle or FIXTURES / "bundle/manifest.json",
        FIXTURES / "companies.csv",
        FIXTURES / "concepts.yml",
        CUTOFF,
    )


def test_identical_build_is_idempotent(tmp_path: Path) -> None:
    first = build_example(tmp_path)
    publish(tmp_path, first["release_id"])
    pointer = (tmp_path / "current.json").read_bytes()
    second = build_example(tmp_path)
    publish(tmp_path, second["release_id"])
    assert first == second
    assert (tmp_path / "current.json").read_bytes() == pointer
    assert len(list((tmp_path / "releases").iterdir())) == 1
    assert second["counts"]["metrics"] == 90


def test_bad_input_cannot_replace_publication(
    tmp_path: Path,
    mutate_bundle: Callable[..., Path],
) -> None:
    root = tmp_path / "warehouse"
    first = build_example(root)
    publish(root, first["release_id"])
    pointer = (root / "current.json").read_bytes()

    def negative_capex(payload: dict[str, Any]) -> None:
        payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["USD"][
            -1
        ]["val"] = -1

    bad = mutate_bundle("9000000001", negative_capex)
    with pytest.raises(ValidationError, match="negative_outflow"):
        build_example(root, bad)
    assert (root / "current.json").read_bytes() == pointer
    assert current_release(root)[1]["release_id"] == first["release_id"]
    assert list((root / "staging").iterdir()) == []
    assert build_example(root)["release_id"] == first["release_id"]


def test_changed_payload_builds_candidate_without_publishing(
    tmp_path: Path,
    mutate_bundle: Callable[..., Path],
) -> None:
    root = tmp_path / "warehouse"
    first = build_example(root)
    publish(root, first["release_id"])

    def new_cash(payload: dict[str, Any]) -> None:
        payload["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"][
            -1
        ]["val"] += 100

    changed = build_example(root, mutate_bundle("9000000001", new_cash))
    assert changed["release_id"] != first["release_id"]
    assert current_release(root)[1]["release_id"] == first["release_id"]


def test_corrupt_release_is_not_published(tmp_path: Path) -> None:
    manifest = build_example(tmp_path)
    directory = release_path(tmp_path, manifest["release_id"])
    (directory / "coverage.json").write_text("[]")
    with pytest.raises(ValidationError, match="checksum"):
        publish(tmp_path, manifest["release_id"])
    assert not (tmp_path / "current.json").exists()


def test_broken_lineage_blocks_validation(
    example: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    root, manifest = example
    working = tmp_path / "broken.duckdb"
    shutil.copyfile(release_path(root, manifest["release_id"]) / "warehouse.duckdb", working)
    with duckdb.connect(str(working)) as con:
        con.execute(
            "DELETE FROM fact_candidates WHERE observation_id IN "
            "(SELECT observation_id FROM selected_facts WHERE status = 'selected' LIMIT 1)"
        )
        with pytest.raises(ValidationError, match="broken_selected_lineage"):
            validate(con)


def test_live_publication_requires_source_reconciliation(
    tmp_path: Path,
    mutate_bundle: Callable[..., Path],
) -> None:
    bundle = mutate_bundle("9000000001", lambda _: None, synthetic=False)
    manifest = build_example(tmp_path / "warehouse", bundle)
    with pytest.raises(ValidationError, match="reconciliation incomplete"):
        publish(tmp_path / "warehouse", manifest["release_id"])


def test_zero_denominator_is_unavailable_not_infinity(
    tmp_path: Path,
    mutate_bundle: Callable[..., Path],
) -> None:
    def zero_revenue(payload: dict[str, Any]) -> None:
        observations = payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        for observation in observations:
            if observation["end"] == "2025-12-31":
                observation["val"] = 0

    root = tmp_path / "warehouse"
    manifest = build_example(root, mutate_bundle("9000000001", zero_revenue))
    with duckdb.connect(
        str(release_path(root, manifest["release_id"]) / "warehouse.duckdb"), read_only=True
    ) as con:
        data = rows(
            con,
            "SELECT value, eligibility_status FROM metrics WHERE cik = '9000000001' "
            "AND period_end = '2025-12-31' AND metric = 'operating_margin'",
        )
    assert data == [{"value": None, "eligibility_status": "invalid_denominator"}]


def test_unreconciled_share_basis_blocks_growth_even_with_consecutive_periods(
    tmp_path: Path,
    mutate_bundle: Callable[..., Path],
) -> None:
    # Give the last period consecutive dates; retain a sourced share-basis flag.
    config = (FIXTURES / "concepts.yml").read_text().replace("2025-02-03", "2025-02-01")
    mapping = tmp_path / "concepts.yml"
    mapping.write_text(config)

    def consecutive(payload: dict[str, Any]) -> None:
        for definition in payload["facts"]["us-gaap"].values():
            for observations in definition["units"].values():
                for observation in observations:
                    if observation.get("start") == "2025-02-03":
                        observation["start"] = "2025-02-01"

    root = tmp_path / "warehouse"
    manifest = build(
        root, mutate_bundle("9000000003", consecutive), FIXTURES / "companies.csv", mapping, CUTOFF
    )
    with duckdb.connect(
        str(release_path(root, manifest["release_id"]) / "warehouse.duckdb"), read_only=True
    ) as con:
        values = {
            r["metric"]: r
            for r in rows(
                con,
                "SELECT metric, value, eligibility_status FROM metrics "
                "WHERE cik = '9000000003' AND period_end = '2026-01-31'",
            )
        }
    assert values["revenue_growth"]["value"] == pytest.approx(25)
    assert values["basic_share_growth"]["eligibility_status"] == "noncomparable"


def test_reproduction_bundle_rebuilds_from_its_own_sources(
    example: tuple[Path, dict[str, Any]],
    tmp_path: Path,
) -> None:
    import tarfile

    root, original = example
    bundle = tmp_path / "release.tar.gz"
    reproduction_bundle(root, bundle)
    with tarfile.open(bundle) as archive:
        archive.extractall(tmp_path / "extracted", filter="data")
    extracted = tmp_path / "extracted" / original["release_id"]
    rebuilt = build(
        tmp_path / "fresh",
        extracted / "sources/manifest.json",
        extracted / "config/companies.csv",
        extracted / "config/concepts.yml",
        CUTOFF,
    )
    assert rebuilt["release_id"] == original["release_id"]
    assert rebuilt["counts"] == original["counts"]
    assert verify_release(tmp_path / "fresh", rebuilt["release_id"])["synthetic"]


def test_interrupted_staging_does_not_hide_current_release(
    example: tuple[Path, dict[str, Any]],
    tmp_path: Path,
) -> None:
    root, manifest = example
    destination = tmp_path / "copy"
    shutil.copytree(root, destination)
    abandoned = destination / "staging" / "interrupted"
    abandoned.mkdir(parents=True)
    (abandoned / "warehouse.duckdb").write_bytes(b"partial")
    assert current_release(destination)[1]["release_id"] == manifest["release_id"]
    assert build_example(destination)["release_id"] == manifest["release_id"]


def test_release_id_cannot_escape_data_directory(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="identifier"):
        publish(tmp_path, "../../outside")


def test_manifests_never_claim_cloud_verification(example: tuple[Path, dict[str, Any]]) -> None:
    _, manifest = example
    assert manifest["cloud_verified"] is False
    assert manifest["synthetic"] is True
    assert "point-in-time" in manifest["basis"]
    assert json.loads(json.dumps(manifest))["counts"]["metrics"] == 90
