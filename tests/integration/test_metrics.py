import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from growth_cost.analysis import inspect_metric, report
from growth_cost.warehouse import release_path, rows


def test_independently_worked_financial_example(example: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = example
    expectations = json.loads(Path("tests/fixtures/expectations.json").read_text())
    with duckdb.connect(
        str(release_path(root, manifest["release_id"]) / "warehouse.duckdb"), read_only=True
    ) as con:
        actual = rows(
            con,
            "SELECT metric, value FROM metrics WHERE cik = ? AND period_end = ?",
            [expectations["cik"], expectations["period_end"]],
        )
    assert {row["metric"]: row["value"] for row in actual} == pytest.approx(expectations["metrics"])


def test_later_comparative_and_exact_duplicates(example: tuple[Path, dict[str, Any]]) -> None:
    root, _ = example
    trace = inspect_metric(root, "9000000001", date(2025, 12, 31), "revenue_growth")
    prior = next(row for row in trace["inputs"] if row["role"] == "prior_revenue")
    assert prior["input_value"] == Decimal("160000000")
    assert prior["filing_date"] == date(2026, 3, 15)
    assert prior["input_period_end"] == date(2024, 12, 31)
    assert "later_comparative" in prior["quality_flags"]
    assert (
        len([r for r in trace["selection_evidence"] if r["period_end"] == date(2024, 12, 31)]) == 2
    )


def test_missing_and_conflicting_inputs_stay_unavailable(
    example: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = example
    cash = inspect_metric(root, "9000000002", date(2025, 12, 31), "free_cash_flow_margin")
    assert cash["inputs"][0]["metric_value"] is None
    investment = next(row for row in cash["inputs"] if row["role"] == "cash_purchases_of_ppe")
    assert investment["input_status"] == "missing"
    assert investment["input_value"] is None
    operating = inspect_metric(root, "9000000002", date(2025, 12, 31), "operating_margin")
    assert operating["inputs"][0]["eligibility_status"] == "ambiguous"
    competing = [r for r in operating["selection_evidence"] if r["canonical"] == "operating_income"]
    assert {r["normalized_value"] for r in competing} == {Decimal(30_000_000), Decimal(31_000_000)}


def test_units_and_duration_are_eligible_before_precedence(
    example: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = example
    trace = inspect_metric(root, "9000000002", date(2025, 12, 31), "basic_share_growth")
    current = next(r for r in trace["inputs"] if r["role"] == "current_basic_shares")
    assert current["input_value"] == Decimal(75_000_000)
    exceptions = report(root, "exceptions")["rows"]
    assert {"conflicting_unit", "instant_not_duration"} <= {r["code"] for r in exceptions}
    revenue = inspect_metric(root, "9000000002", date(2025, 12, 31), "revenue_growth")
    assert revenue["inputs"][0]["metric_value"] == pytest.approx(20)


def test_growth_is_not_calculated_across_reporting_gap(
    example: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = example
    trace = inspect_metric(root, "9000000003", date(2026, 1, 31), "revenue_growth")
    assert trace["inputs"][0]["metric_value"] is None
    assert trace["inputs"][0]["eligibility_status"] == "noncomparable"
    margin = inspect_metric(root, "9000000003", date(2026, 1, 31), "operating_margin")
    assert margin["inputs"][0]["metric_value"] == pytest.approx(-100 / 6)


def test_every_available_metric_has_all_source_links(example: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = example
    database = release_path(root, manifest["release_id"]) / "warehouse.duckdb"
    with duckdb.connect(str(database), read_only=True) as con:
        lineage = rows(con, "SELECT * FROM metric_lineage WHERE eligibility_status = 'available'")
        assert len(lineage) > 100
        for row in lineage:
            for field in (
                "selected_fact_id",
                "observation_id",
                "accession",
                "snapshot_id",
                "content_hash",
                "primary_document_url",
                "inventory_snapshot_id",
            ):
                assert row[field] is not None, (row["metric_id"], field)


def test_overview_discloses_window_and_keeps_absent_companies(
    example: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = example
    output = report(root, "overview", window_start=date(2026, 1, 1), window_end=date(2026, 1, 31))
    absent = [r for r in output["rows"] if r["cik"] == "9000000001"]
    assert len(absent) == 6
    assert all(r["value"] is None and r["eligibility_status"] == "missing" for r in absent)
    assert output["synthetic"] is True


def test_overview_does_not_call_input_only_year_missing_data(
    example: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = example
    output = report(root, "overview", window_start=date(2020, 12, 1), window_end=date(2020, 12, 31))
    input_only = [r for r in output["rows"] if r["cik"] == "9000000001"]
    assert input_only
    assert all(r["period_end"] is None and r["value"] is None for r in input_only)
    assert {r["unavailable_reason"] for r in input_only} == {"No output period in window"}


def test_acquisition_sensitivity_compares_remaining_years_and_stays_labelled(
    example: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = example
    trends = report(root, "trend_summaries")["rows"]
    base = next(
        r
        for r in trends
        if r["cik"] == "9000000002"
        and r["metric"] == "revenue_growth"
        and r["window_years"] == 5
        and not r["exclude_acquisition_years"]
    )
    sensitivity = next(
        r
        for r in trends
        if r["cik"] == "9000000002"
        and r["metric"] == "revenue_growth"
        and r["window_years"] == 5
        and r["exclude_acquisition_years"]
    )
    assert base["complete"]
    assert base["excluded_periods"] == 0
    # Growth 100, 100, 12.5, 11.11..., 20: fiscal 2022 and 2023 both touch the flagged year.
    assert base["mean_value"] == pytest.approx((100 + 100 + 12.5 + 100 / 9 + 20) / 5)
    assert sensitivity["excluded_periods"] == 2
    assert sensitivity["periods"] == 3
    assert sensitivity["mean_value"] == pytest.approx((100 + 100 / 9 + 20) / 3)
    # The shortened window is reported as a number, never as a complete five-year result.
    assert not sensitivity["complete"]
