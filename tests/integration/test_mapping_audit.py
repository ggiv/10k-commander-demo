"""Each mapping audit check fires on a hand-made tagging problem in the fixture."""

import copy
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from growth_cost.analysis import report
from growth_cost.pipeline import build

FIXTURES = Path("tests/fixtures")
CUTOFF = date(2026, 11, 4)
CIK = "9000000001"
END = "2025-12-31"
PPE = "PaymentsToAcquirePropertyPlantAndEquipment"


def findings(root: Path, bundle: Path, mappings: Path | None = None) -> list[dict[str, Any]]:
    manifest = build(
        root, bundle, FIXTURES / "companies.csv", mappings or FIXTURES / "concepts.yml", CUTOFF
    )
    rows: list[dict[str, Any]] = report(root, "mapping_audit", release_id=manifest["release_id"])[
        "rows"
    ]
    return rows


def matching(rows: list[dict[str, Any]], check: str, canonical: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["check_name"] == check
        and row["cik"] == CIK
        and str(row["period_end"]) == END
        and row["canonical"] == canonical
    ]


def observations(payload: dict[str, Any], concept: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = payload["facts"]["us-gaap"][concept]["units"]["USD"]
    return found


def ending(payload: dict[str, Any], concept: str) -> list[dict[str, Any]]:
    return [o for o in observations(payload, concept) if o["end"] == END and "start" in o]


def test_alternate_revenue_tag_that_disagrees_is_reported(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def add_larger_total(payload: dict[str, Any]) -> None:
        larger = [dict(o, val=o["val"] + 20_000_000) for o in ending(payload, "Revenues")]
        payload["facts"]["us-gaap"]["SalesRevenueNet"] = {"units": {"USD": larger}}

    rows = findings(tmp_path / "w", mutate_bundle(CIK, add_larger_total))
    found = matching(rows, "alternate_tag_differs", "revenue")
    assert [row["other_concept"] for row in found] == ["SalesRevenueNet"]


def test_alternate_revenue_tag_that_agrees_is_not_reported(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def add_equal_total(payload: dict[str, Any]) -> None:
        same = copy.deepcopy(ending(payload, "Revenues"))
        payload["facts"]["us-gaap"]["SalesRevenueNet"] = {"units": {"USD": same}}

    rows = findings(tmp_path / "w", mutate_bundle(CIK, add_equal_total))
    assert not matching(rows, "alternate_tag_differs", "revenue")


def test_capex_equal_to_an_acquisition_line_is_reported(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def add_acquisition(payload: dict[str, Any]) -> None:
        same = copy.deepcopy(ending(payload, PPE))
        payload["facts"]["us-gaap"]["PaymentsToAcquireBusinessesNetOfCashAcquired"] = {
            "units": {"USD": same}
        }

    rows = findings(tmp_path / "w", mutate_bundle(CIK, add_acquisition))
    assert matching(rows, "capex_matches_acquisition_line", "capex")


def test_selected_concept_change_between_years_is_reported(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    fallback = "PaymentsToAcquireProductiveAssets"

    def move_latest_year(payload: dict[str, Any]) -> None:
        latest = ending(payload, PPE)
        payload["facts"]["us-gaap"][fallback] = {"units": {"USD": copy.deepcopy(latest)}}
        kept = [o for o in observations(payload, PPE) if o not in latest]
        payload["facts"]["us-gaap"][PPE]["units"]["USD"] = kept

    config = yaml.safe_load((FIXTURES / "concepts.yml").read_text())
    config["mappings"].append(
        {
            "canonical": "capex",
            "taxonomy": "us-gaap",
            "concept": fallback,
            "unit": "USD",
            "priority": 2,
        }
    )
    mappings = tmp_path / "concepts.yml"
    mappings.write_text(yaml.safe_dump(config))
    rows = findings(tmp_path / "w", mutate_bundle(CIK, move_latest_year), mappings)
    found = matching(rows, "selected_concept_changed", "capex")
    assert [row["concept"] for row in found] == [fallback]
    assert [row["other_concept"] for row in found] == [PPE]


def test_jump_without_matching_revenue_change_is_reported(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def multiply_capex(payload: dict[str, Any]) -> None:
        for observation in ending(payload, PPE):
            observation["val"] *= 5

    rows = findings(tmp_path / "w", mutate_bundle(CIK, multiply_capex))
    assert matching(rows, "unexplained_jump", "capex")
