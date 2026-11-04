"""Read-only reports, metric inspection, and release exports."""

from __future__ import annotations

import json
import tarfile
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from growth_cost.contracts import ContractError, json_bytes
from growth_cost.sec import atomic_write
from growth_cost.warehouse import current_release, release_path, rows, sql_file, verify_release


def resolve_release(root: Path, release_id: str | None) -> tuple[Path, dict[str, Any]]:
    """A named candidate or published release, otherwise the current publication."""
    if release_id:
        return release_path(root, release_id), verify_release(root, release_id)
    return current_release(root)


def report(
    root: Path,
    kind: str,
    *,
    release_id: str | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    business_model: str | None = None,
) -> dict[str, Any]:
    path, manifest = resolve_release(root, release_id)
    with duckdb.connect(str(path / "warehouse.duckdb"), read_only=True) as connection:
        if kind == "reconciliation_inputs":
            data = rows(connection, sql_file("reconciliation_inputs.sql"))
        elif kind == "mapping_audit":
            data = rows(connection, sql_file("mapping_audit.sql"))
        elif kind in {
            "coverage",
            "exceptions",
            "trend_summaries",
            "metric_definitions",
            "annotations",
        }:
            data = rows(connection, f"SELECT * FROM {kind}")
        elif kind == "overview":
            if window_start is None or window_end is None or window_start > window_end:
                raise ContractError("Overview requires an explicit, ordered reporting-end window")
            data = rows(
                connection,
                """
                WITH chosen AS (
                    -- The oldest input-only year supports growth but has no output metrics.
                    SELECT * FROM periods p WHERE period_end BETWEEN ? AND ? AND EXISTS (
                        SELECT 1 FROM metrics m
                        WHERE m.cik = p.cik AND m.period_end = p.period_end
                    )
                    QUALIFY row_number() OVER (PARTITION BY cik ORDER BY period_end DESC) = 1
                ), displayed AS (
                    SELECT c.cik, c.ticker, c.name, c.business_model,
                           p.period_start, p.period_end, p.duration_days,
                           coalesce(m.metric, d.metric) AS metric, m.value,
                           coalesce(m.eligibility_status, 'missing') AS eligibility_status,
                           CASE WHEN p.cik IS NULL THEN 'No output period in window'
                                ELSE m.unavailable_reason END AS unavailable_reason,
                           coalesce(m.quality_flags, '') AS quality_flags,
                           d.label, d.unit, d.formula, d.interpretation,
                           m.metric_id, ? AS window_start, ? AS window_end
                    FROM companies c CROSS JOIN metric_definitions d
                    LEFT JOIN chosen p ON p.cik = c.cik
                    LEFT JOIN metrics m ON c.cik = m.cik AND p.period_start = m.period_start
                        AND p.period_end = m.period_end AND d.metric = m.metric
                    WHERE c.included AND (? IS NULL OR c.business_model = ?)
                )
                SELECT *, date_diff('day', period_end, max(period_end) OVER ()) > 90
                    AS materially_different_reporting_window
                FROM displayed ORDER BY cik, metric
            """,
                [
                    window_start,
                    window_end,
                    window_start,
                    window_end,
                    business_model,
                    business_model,
                ],
            )
        else:
            raise ContractError("Unknown report")
    return {
        "release_id": manifest["release_id"],
        "synthetic": manifest["synthetic"],
        "cutoff": manifest["cutoff"],
        "basis": manifest["basis"],
        "rows": data,
    }


def inspect_metric(
    root: Path,
    cik: str,
    period_end: date,
    metric: str,
    *,
    release_id: str | None = None,
) -> dict[str, Any]:
    path, manifest = resolve_release(root, release_id)
    with duckdb.connect(str(path / "warehouse.duckdb"), read_only=True) as connection:
        definitions = rows(
            connection, "SELECT * FROM metric_definitions WHERE metric = ?", [metric]
        )
        data = rows(
            connection,
            """
            SELECT * FROM metric_lineage WHERE cik = ? AND period_end = ? AND metric = ?
            ORDER BY role
        """,
            [cik, period_end, metric],
        )
        if not data:
            raise ContractError("Metric is not present in this release")
        evidence = rows(
            connection,
            """
            SELECT e.*, sn.storage_ref, sn.url AS captured_source_url,
                   sn.content_hash, sn.retrieved_at FROM selection_candidates e
            JOIN selected_facts s USING (cik, period_start, period_end, canonical, unit)
            JOIN metric_inputs i USING (selected_fact_id)
            JOIN metrics m USING (metric_id)
            JOIN source_snapshots sn ON e.snapshot_id = sn.snapshot_id
            WHERE m.cik = ? AND m.period_end = ? AND m.metric = ?
            ORDER BY e.canonical, e.period_end, e.observation_id
        """,
            [cik, period_end, metric],
        )
        annotations = rows(
            connection,
            """
            SELECT DISTINCT a.* FROM annotations a
            JOIN metric_inputs i ON a.cik = i.cik
            LEFT JOIN selected_facts s USING (selected_fact_id)
            WHERE i.cik = ? AND i.period_end = ? AND i.metric = ?
                AND a.period_start = s.period_start AND a.period_end = s.period_end
        """,
            [cik, period_end, metric],
        )
    return {
        "release_id": manifest["release_id"],
        "synthetic": manifest["synthetic"],
        "inputs": data,
        "definition": definitions[0] if definitions else None,
        "selection_evidence": evidence,
        "annotations": annotations,
        "source_archive": str((path / "sources").resolve()),
    }


def export_release(root: Path, destination: Path) -> dict[str, Any]:
    path, manifest = current_release(root)
    destination = destination.resolve()
    if destination.is_relative_to(path.resolve()):
        raise ContractError("Export destination must be outside the immutable release")
    destination.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(path / "warehouse.duckdb"), read_only=True) as connection:
        for table in (
            "metrics",
            "metric_definitions",
            "metric_lineage",
            "coverage",
            "trend_summaries",
        ):
            atomic_write(
                destination / f"{table}.json",
                json_bytes(
                    {
                        "release_id": manifest["release_id"],
                        "synthetic": manifest["synthetic"],
                        "rows": rows(connection, f"SELECT * FROM {table}"),
                    }
                ),
            )
    atomic_write(destination / "manifest.json", json_bytes(manifest))
    return {"release_id": manifest["release_id"], "destination": str(destination)}


def reproduction_bundle(root: Path, destination: Path) -> dict[str, Any]:
    path, manifest = current_release(root)
    if destination.resolve().is_relative_to(path.resolve()):
        raise ContractError("Bundle must be outside the immutable release")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(path, arcname=path.name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "release_id": manifest["release_id"],
        "archive": str(destination.resolve()),
        "bytes": destination.stat().st_size,
    }


def stored_manifest(root: Path) -> dict[str, Any]:
    _, manifest = current_release(root)
    # Return ordinary JSON values rather than driver-specific date/decimal objects.
    return dict(json.loads(json_bytes(manifest)))
