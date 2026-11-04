"""Selection edge cases with hand-worked values from the Example Applications fixture."""

import copy
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from growth_cost.analysis import report
from growth_cost.contracts import ContractError
from growth_cost.pipeline import build
from growth_cost.warehouse import ValidationError, publish, release_path, rows

FIXTURES = Path("tests/fixtures")
CUTOFF = date(2026, 11, 4)
CIK = "9000000001"


def revenue_observations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    return observations


def observation_ending(observations: list[dict[str, Any]], end: str) -> dict[str, Any]:
    return next(o for o in observations if o["end"] == end)


def config_with(tmp_path: Path, *extra: dict[str, Any], demote_revenue: bool = False) -> Path:
    config = yaml.safe_load((FIXTURES / "concepts.yml").read_text())
    if demote_revenue:
        for mapping in config["mappings"]:
            if mapping["canonical"] == "revenue":
                mapping["priority"] = 2
    config["mappings"].extend(extra)
    path = tmp_path / "concepts.yml"
    path.write_text(yaml.safe_dump(config))
    return path


def build_release(root: Path, bundle: Path, mappings: Path | None = None) -> dict[str, Any]:
    return build(
        root, bundle, FIXTURES / "companies.csv", mappings or FIXTURES / "concepts.yml", CUTOFF
    )


def query(root: Path, manifest: dict[str, Any], sql: str, *params: Any) -> list[dict[str, Any]]:
    database = release_path(root, manifest["release_id"]) / "warehouse.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        return rows(connection, sql, list(params) or None)


def metric_value(root: Path, manifest: dict[str, Any], metric: str, end: str) -> Any:
    found = query(
        root,
        manifest,
        "SELECT value FROM metrics WHERE cik = ? AND period_end = ? AND metric = ?",
        CIK,
        end,
        metric,
    )
    return found[0]["value"]


def input_value(root: Path, manifest: dict[str, Any], canonical: str, end: str) -> Any:
    found = query(
        root,
        manifest,
        "SELECT value FROM selected_facts WHERE cik = ? AND period_end = ? AND canonical = ?",
        CIK,
        end,
        canonical,
    )
    return found[0]["value"]


def exception_codes(root: Path, manifest: dict[str, Any]) -> set[str]:
    found = query(root, manifest, "SELECT code FROM exceptions WHERE cik = ?", CIK)
    return {row["code"] for row in found}


def test_unmapped_unrepresentable_fact_keeps_raw_value_without_stopping_build(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def add_ratio(payload: dict[str, Any]) -> None:
        observation = copy.deepcopy(revenue_observations(payload)[0])
        observation["val"] = 0.1234567
        payload["facts"]["us-gaap"]["SomeUnmappedRatio"] = {"units": {"pure": [observation]}}

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, add_ratio))
    stored = query(
        root,
        manifest,
        "SELECT value, raw_value FROM fact_candidates WHERE concept = 'SomeUnmappedRatio'",
    )
    assert stored == [{"value": None, "raw_value": "0.1234567"}]


def test_mapped_unrepresentable_fact_blocks_rather_than_selecting_stale_value(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def overprecise(payload: dict[str, Any]) -> None:
        observation_ending(revenue_observations(payload), "2025-12-31")["val"] = 0.1234567

    with pytest.raises(ValidationError, match="mapped_value_not_representable"):
        build_release(tmp_path / "warehouse", mutate_bundle(CIK, overprecise))


def test_unmapped_filing_metadata_mismatch_does_not_block_or_report(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def add_mismatch(payload: dict[str, Any]) -> None:
        observation = copy.deepcopy(revenue_observations(payload)[0])
        observation["filed"] = "2021-03-16"
        payload["facts"]["us-gaap"]["SomeUnmappedConcept"] = {"units": {"USD": [observation]}}

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, add_mismatch))
    assert "filing_metadata_mismatch" not in exception_codes(root, manifest)


def test_mapped_filing_metadata_mismatch_uses_inventory_and_is_reported(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def mismatch(payload: dict[str, Any]) -> None:
        observation_ending(revenue_observations(payload), "2025-12-31")["filed"] = "2026-03-16"

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, mismatch))
    assert input_value(root, manifest, "revenue", "2025-12-31") == 200_000_000
    assert "filing_metadata_mismatch" in exception_codes(root, manifest)


def test_conflicting_period_definition_withholds_one_year_for_one_company(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def off_by_one(payload: dict[str, Any]) -> None:
        observations = revenue_observations(payload)
        observation = copy.deepcopy(observation_ending(observations, "2023-12-31"))
        observation["start"] = "2023-01-02"
        observations.append(observation)

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, off_by_one))
    assert "conflicting_period_definition" in exception_codes(root, manifest)
    periods = query(
        root, manifest, "SELECT count(*) AS n FROM periods WHERE period_end = '2023-12-31'"
    )
    assert periods == [{"n": 1}]  # only the sibling calendar-year fixture company remains
    assert metric_value(root, manifest, "revenue_growth", "2025-12-31") == pytest.approx(25)
    following = query(
        root,
        manifest,
        "SELECT eligibility_status FROM metrics "
        "WHERE cik = ? AND period_end = '2024-12-31' AND metric = 'revenue_growth'",
        CIK,
    )
    assert following == [{"eligibility_status": "noncomparable"}]


def test_later_value_under_lower_priority_concept_is_flagged(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def older_concept(payload: dict[str, Any]) -> None:
        original = [
            o
            for o in revenue_observations(payload)
            if o["end"] == "2024-12-31" and o["val"] == 150_000_000
        ]
        payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
            "units": {"USD": original}
        }

    root = tmp_path / "warehouse"
    mappings = config_with(
        tmp_path,
        {
            "canonical": "revenue",
            "taxonomy": "us-gaap",
            "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
            "unit": "USD",
            "priority": 1,
        },
        demote_revenue=True,
    )
    manifest = build_release(root, mutate_bundle(CIK, older_concept), mappings)
    # Priority still decides: 200m / 150m. The later 160m is surfaced, not silently lost.
    assert metric_value(root, manifest, "revenue_growth", "2025-12-31") == pytest.approx(100 / 3)
    assert "later_value_under_lower_priority_concept" in exception_codes(root, manifest)
    flags = query(
        root,
        manifest,
        "SELECT quality_flags FROM metrics "
        "WHERE cik = ? AND period_end = '2025-12-31' AND metric = 'revenue_growth'",
        CIK,
    )
    assert "later_value_under_lower_priority_concept" in flags[0]["quality_flags"]


def test_scoped_sign_override_applies_only_when_it_wins(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def negative_capex(payload: dict[str, Any]) -> None:
        concept = payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
        for observation in concept["units"]["USD"]:
            observation["val"] = -observation["val"]

    mappings = config_with(
        tmp_path,
        {
            "canonical": "capex",
            "taxonomy": "us-gaap",
            "concept": "PaymentsToAcquirePropertyPlantAndEquipment",
            "unit": "USD",
            "priority": 0,
            "cik": CIK,
            "multiplier": -1,
            "source_url": "https://example.invalid/fixture/sign",
            "explanation": "Synthetic filer reports the outflow as negative.",
        },
    )
    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, negative_capex), mappings)
    assert input_value(root, manifest, "capex", "2025-12-31") == 10_000_000
    assert metric_value(root, manifest, "free_cash_flow_margin", "2025-12-31") == pytest.approx(30)


def test_dated_mapping_only_applies_inside_its_range(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def retired_concept(payload: dict[str, Any]) -> None:
        capex = payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
        inside = copy.deepcopy(observation_ending(capex["units"]["USD"], "2023-12-31"))
        inside["val"] = 7_000_000
        outside = copy.deepcopy(observation_ending(capex["units"]["USD"], "2025-12-31"))
        outside["val"] = 999_000_000
        payload["facts"]["us-gaap"]["RetiredCapexConcept"] = {"units": {"USD": [inside, outside]}}

    mappings = config_with(
        tmp_path,
        {
            "canonical": "capex",
            "taxonomy": "us-gaap",
            "concept": "RetiredCapexConcept",
            "unit": "USD",
            "priority": 0,
            "cik": CIK,
            "valid_to": "2023-12-31",
            "source_url": "https://example.invalid/fixture/retired",
            "explanation": "Synthetic concept used through fiscal 2023.",
        },
    )
    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, retired_concept), mappings)
    assert input_value(root, manifest, "capex", "2023-12-31") == 7_000_000
    assert input_value(root, manifest, "capex", "2025-12-31") == 10_000_000


def test_later_amendment_takes_precedence_over_original_filing(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    accession = f"{CIK}-26-000099"

    def amended_fact(payload: dict[str, Any]) -> None:
        observations = revenue_observations(payload)
        amended = copy.deepcopy(observation_ending(observations, "2025-12-31"))
        amended.update(val=210_000_000, accn=accession, form="10-K/A", filed="2026-06-01")
        observations.append(amended)

    def amended_filing(payload: dict[str, Any]) -> None:
        columns = payload["filings"]["recent"]
        columns["accessionNumber"].append(accession)
        columns["filingDate"].append("2026-06-01")
        columns["acceptanceDateTime"].append("2026-06-01T12:00:00Z")
        columns["reportDate"].append("2025-12-31")
        columns["form"].append("10-K/A")
        columns["primaryDocument"].append("synthetic-10ka.htm")

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, amended_fact, submissions=amended_filing))
    assert input_value(root, manifest, "revenue", "2025-12-31") == 210_000_000
    assert metric_value(root, manifest, "revenue_growth", "2025-12-31") == pytest.approx(31.25)


def add_orphan_revenue(payload: dict[str, Any], **changes: Any) -> None:
    """A revenue fact citing an accession that the filing inventory does not list."""
    observations = revenue_observations(payload)
    orphan = copy.deepcopy(observation_ending(observations, "2023-12-31"))
    orphan.update(accn=f"{CIK}-24-000099", **changes)
    observations.append(orphan)


def test_harmless_orphan_filing_is_reported_without_blocking(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    # Older than the selected 2024-03-15 filing, same value: it cannot change the result.
    bundle = mutate_bundle(CIK, lambda p: add_orphan_revenue(p, filed="2024-03-14"))
    root = tmp_path / "warehouse"
    manifest = build_release(root, bundle)
    assert input_value(root, manifest, "revenue", "2023-12-31") == 140_000_000
    assert "filing_not_in_inventory" in exception_codes(root, manifest)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"filed": "2024-03-14", "val": 141_000_000}, id="different_value"),
        pytest.param({"filed": "2024-03-16"}, id="later_filing_same_value"),
        pytest.param(
            {"start": "2025-11-01", "end": "2026-10-31", "filed": "2026-10-31"},
            id="period_with_no_selected_fact",
        ),
    ],
)
def test_orphan_filing_that_could_change_the_result_blocks(
    tmp_path: Path, mutate_bundle: Callable[..., Path], changes: dict[str, Any]
) -> None:
    bundle = mutate_bundle(CIK, lambda p: add_orphan_revenue(p, **changes))
    with pytest.raises(ValidationError, match="mapped_annual_filing_missing_inventory"):
        build_release(tmp_path / "warehouse", bundle)


def test_orphan_filing_under_a_preferred_concept_blocks(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def preferred_orphan(payload: dict[str, Any]) -> None:
        orphan = copy.deepcopy(observation_ending(revenue_observations(payload), "2023-12-31"))
        orphan.update(accn=f"{CIK}-24-000099", filed="2024-03-14")
        payload["facts"]["us-gaap"]["PreferredRevenueConcept"] = {"units": {"USD": [orphan]}}

    mappings = config_with(
        tmp_path,
        {
            "canonical": "revenue",
            "taxonomy": "us-gaap",
            "concept": "PreferredRevenueConcept",
            "unit": "USD",
            "priority": 0,
        },
    )
    with pytest.raises(ValidationError, match="mapped_annual_filing_missing_inventory"):
        build_release(tmp_path / "warehouse", mutate_bundle(CIK, preferred_orphan), mappings)


def test_orphan_filing_under_a_lower_priority_concept_cannot_block(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def fallback_orphan(payload: dict[str, Any]) -> None:
        orphan = copy.deepcopy(observation_ending(revenue_observations(payload), "2023-12-31"))
        orphan.update(accn=f"{CIK}-24-000099", filed="2024-03-16", val=1)
        payload["facts"]["us-gaap"]["FallbackRevenueConcept"] = {"units": {"USD": [orphan]}}

    mappings = config_with(
        tmp_path,
        {
            "canonical": "revenue",
            "taxonomy": "us-gaap",
            "concept": "FallbackRevenueConcept",
            "unit": "USD",
            "priority": 2,
        },
    )
    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, fallback_orphan), mappings)
    assert input_value(root, manifest, "revenue", "2023-12-31") == 140_000_000
    assert "filing_not_in_inventory" in exception_codes(root, manifest)


@pytest.mark.parametrize("shares", [1_000, 1_000_000_000])
def test_implausible_share_jump_is_withheld_not_published(
    tmp_path: Path, mutate_bundle: Callable[..., Path], shares: int
) -> None:
    # 108m shares before: a scale error down or a split up, neither is share growth.
    def jump(payload: dict[str, Any]) -> None:
        concept = payload["facts"]["us-gaap"]["WeightedAverageNumberOfSharesOutstandingBasic"]
        observation_ending(concept["units"]["shares"], "2025-12-31")["val"] = shares

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, jump))
    found = query(
        root,
        manifest,
        "SELECT value, eligibility_status, unavailable_reason FROM metrics "
        "WHERE cik = ? AND period_end = '2025-12-31' AND metric = 'basic_share_growth'",
        CIK,
    )
    assert found[0]["value"] is None
    assert found[0]["eligibility_status"] == "noncomparable"
    assert "50%" in found[0]["unavailable_reason"]
    assert metric_value(root, manifest, "revenue_growth", "2025-12-31") == pytest.approx(25)


def test_negative_stock_compensation_is_flagged_without_blocking(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    def reversal(payload: dict[str, Any]) -> None:
        concept = payload["facts"]["us-gaap"]["ShareBasedCompensation"]
        observation_ending(concept["units"]["USD"], "2025-12-31")["val"] = -20_000_000

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, reversal))
    assert metric_value(root, manifest, "stock_compensation_intensity", "2025-12-31") == -10
    flags = query(
        root,
        manifest,
        "SELECT quality_flags FROM metrics WHERE cik = ? AND period_end = '2025-12-31' "
        "AND metric = 'stock_compensation_intensity'",
        CIK,
    )
    assert "negative_stock_compensation" in flags[0]["quality_flags"]
    assert any(
        check["check_name"] == "negative_stock_compensation" for check in manifest["validation"]
    )


def test_cutoffs_with_no_new_source_dates_reuse_one_release(tmp_path: Path) -> None:
    bundle = FIXTURES / "bundle/manifest.json"
    args = (bundle, FIXTURES / "companies.csv", FIXTURES / "concepts.yml")
    first = build(tmp_path, *args, date(2026, 11, 2))
    second = build(tmp_path, *args, date(2026, 11, 9))
    assert first["release_id"] == second["release_id"]
    assert len(list((tmp_path / "releases").iterdir())) == 1
    assert first["cutoff"] == "2026-03-15"
    assert first["requested_cutoff"] == "2026-11-02"


def test_cutoff_past_a_new_filing_builds_a_new_release(tmp_path: Path) -> None:
    bundle = FIXTURES / "bundle/manifest.json"
    args = (bundle, FIXTURES / "companies.csv", FIXTURES / "concepts.yml")
    before = build(tmp_path, *args, CUTOFF)
    after = build(tmp_path, *args, date(2027, 12, 31))
    assert before["release_id"] != after["release_id"]
    assert after["cutoff"] == "2027-03-15"


def test_cutoff_before_all_sources_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="on or before cutoff"):
        build(
            tmp_path,
            FIXTURES / "bundle/manifest.json",
            FIXTURES / "companies.csv",
            FIXTURES / "concepts.yml",
            date(2000, 1, 1),
        )


def test_large_change_review_compares_periods_not_levels(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    income_by_end = {"2024-12-31": -192_000_000, "2025-12-31": -240_000_000}

    def stable_loss(payload: dict[str, Any]) -> None:
        concept = payload["facts"]["us-gaap"]["OperatingIncomeLoss"]
        for observation in concept["units"]["USD"]:
            observation["val"] = income_by_end.get(observation["end"], observation["val"])

    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, stable_loss))
    flags = {
        str(row["period_end"]): row["quality_flags"]
        for row in query(
            root,
            manifest,
            "SELECT period_end, quality_flags FROM metrics "
            "WHERE cik = ? AND metric = 'operating_margin'",
            CIK,
        )
    }
    # 2023 margin 20% to 2024 margin -120% is a swing; -120% to -120% is not.
    assert "large_change_review" in flags["2024-12-31"]
    assert "large_change_review" not in flags["2025-12-31"]


def test_report_lists_candidate_inputs_needing_reconciliation(
    tmp_path: Path, mutate_bundle: Callable[..., Path]
) -> None:
    root = tmp_path / "warehouse"
    manifest = build_release(root, mutate_bundle(CIK, lambda _: None, synthetic=False))
    with pytest.raises(ValidationError, match="reconciliation incomplete"):
        publish(root, manifest["release_id"])
    needed = report(root, "reconciliation_inputs", release_id=manifest["release_id"])["rows"]
    assert needed
    assert not any(row["reconciled"] for row in needed)
    revenue = next(
        row
        for row in needed
        if row["cik"] == CIK
        and row["canonical"] == "revenue"
        and str(row["period_end"]) == "2025-12-31"
    )
    assert revenue["value"] == 200_000_000
    assert revenue["source_url"].endswith("synthetic-10k.htm")
