"""Database loading, validation, and atomic local publication."""

from __future__ import annotations

import csv
import fcntl
import json
import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import astuple
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import duckdb

from growth_cost.contracts import (
    Configuration,
    ContractError,
    digest,
    financial_decimal,
    json_bytes,
)
from growth_cost.normalize import Fact, Filing
from growth_cost.sec import Archive, atomic_write, read_bundle

SQL_DIR = Path(__file__).parent / "sql"
DEFINITION_VERSION = "annual-v1"


class ValidationError(ContractError):
    pass


def sql_file(name: str) -> str:
    return (SQL_DIR / name).read_text()


def rows(
    connection: duckdb.DuckDBPyConnection, query: str, params: Any = None
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def insert_records(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    records: Sequence[Any],
) -> None:
    """Bulk load through CSV; executemany manages only about 1,000 rows per second."""
    if not records:
        return
    null_marker = r"\N"

    def cell(value: Any) -> Any:
        if value is None:
            return null_marker
        return format(value, "f") if isinstance(value, Decimal) else value

    with TemporaryDirectory() as directory:
        path = Path(directory) / "records.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows([cell(value) for value in astuple(record)] for record in records)
        location = str(path).replace("'", "''")
        connection.execute(
            f"COPY {table} FROM '{location}' "
            f"(FORMAT CSV, HEADER false, QUOTE '\"', ESCAPE '\"', NULLSTR '{null_marker}')"
        )


def load_database(
    connection: duckdb.DuckDBPyConnection,
    config: Configuration,
    snapshots: Sequence[Any],
    filings: list[Filing],
    facts: list[Fact],
    release: Sequence[Any],
    reconciliation: Path | None,
) -> None:
    connection.execute(sql_file("schema.sql"))
    connection.execute("BEGIN TRANSACTION")
    try:
        insert_records(connection, "companies", config.companies)
        insert_records(connection, "mappings", config.mappings)
        insert_records(connection, "annotations", config.annotations)
        insert_records(connection, "source_snapshots", snapshots)
        insert_records(connection, "filings", filings)
        insert_records(connection, "fact_candidates", facts)
        connection.execute("INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", release)
        if reconciliation:
            with reconciliation.open(newline="") as handle:
                for row in csv.DictReader(handle):
                    value = financial_decimal(Decimal(row["value"]))
                    connection.execute(
                        "INSERT INTO reconciliation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            row["cik"],
                            row["period_start"],
                            row["period_end"],
                            row["canonical"],
                            value,
                            row["unit"],
                            row["source_url"],
                            row["statement_reference"],
                            row["reviewer"],
                            row["reviewed_on"],
                            row["scope_notes"],
                        ],
                    )
        connection.execute(sql_file("selected_facts.sql"))
        connection.execute(sql_file("metrics.sql"))
        connection.execute(sql_file("checks.sql"))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def validate(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    query = sql_file("checks.sql").split("CREATE TABLE validation_results AS\n", 1)[1]
    results = rows(connection, query)
    blockers = [r for r in results if r["severity"] == "blocker"]
    if blockers:
        logging.getLogger("growth_cost").error(
            json.dumps(
                {
                    "event": "validation_failed",
                    "checks": blockers,
                }
            )
        )
        raise ValidationError(json.dumps(blockers))
    return results


def feasibility_gate(
    connection: duckdb.DuckDBPyConnection, *, allow_unreviewed: bool = False
) -> None:
    """Live publication requires reviewed mappings unless it is explicitly marked unreviewed."""
    release = rows(connection, "SELECT * FROM releases")[0]
    if release["synthetic"]:
        return
    if not release["mapping_reviewed"]:
        if allow_unreviewed:
            return
        raise ValidationError(
            "Live mappings are unreviewed; complete the filing reconciliation first"
        )
    companies = connection.execute("SELECT count(*) FROM companies WHERE included").fetchone()
    assert companies is not None
    if companies[0] < 3:
        raise ValidationError("Live feasibility gate requires at least three companies")
    inputs = rows(connection, sql_file("reconciliation_inputs.sql"))
    unresolved = [row for row in inputs if not row["reconciled"]]
    incomplete = rows(
        connection,
        """
        WITH recent AS (
            SELECT *, dense_rank() OVER (PARTITION BY cik ORDER BY period_end DESC) AS recency
            FROM metrics
        )
        SELECT c.cik FROM companies c LEFT JOIN recent m ON c.cik = m.cik AND m.recency <= 2
        WHERE c.included GROUP BY c.cik
        HAVING count(m.metric_id) <> 12 OR count(m.value) <> 12
    """,
    )
    if unresolved or incomplete:
        raise ValidationError(
            f"Pilot reconciliation incomplete: {len(unresolved)} inputs and "
            f"{len(incomplete)} companies need review"
        )


@contextmanager
def publication_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".publication.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def release_path(root: Path, release_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", release_id):
        raise ContractError("Invalid release identifier")
    return root / "releases" / release_id


def verify_release(root: Path, release_id: str) -> dict[str, Any]:
    path = release_path(root, release_id)
    manifest: dict[str, Any] = json.loads((path / "manifest.json").read_bytes())
    if manifest["release_id"] != release_id:
        raise ValidationError("Release manifest identifier mismatch")
    required = {
        "warehouse.duckdb",
        "sources/manifest.json",
        "config/companies.csv",
        "config/concepts.yml",
        "coverage.json",
        "exceptions.json",
        "validation.json",
    }
    if not required <= manifest["artifacts"].keys():
        raise ValidationError("Release manifest omits required artifact checksums")
    expected_id = digest(
        json_bytes(
            {
                "cutoff": manifest["cutoff"],
                "code": manifest["code_hash"],
                "config": manifest["config_hash"],
                "sources": manifest["source_manifest_hash"],
                "synthetic": manifest["synthetic"],
                "definitions": manifest["definition_version"],
            }
        )
    )[:24]
    if expected_id != release_id:
        raise ValidationError("Release identity does not match its manifest")
    for name, expected in manifest["artifacts"].items():
        target = (path / name).resolve()
        if not target.is_relative_to(path.resolve()) or digest(target.read_bytes()) != expected:
            raise ValidationError("Release artifact checksum mismatch")
    archive, snapshots, _ = read_bundle(path / "sources" / "manifest.json")
    for snapshot in snapshots:
        archive.payload(snapshot)
    with duckdb.connect(str(path / "warehouse.duckdb"), read_only=True) as connection:
        stored = rows(connection, "SELECT release_id, synthetic, cutoff FROM releases")
        if len(stored) != 1 or stored[0]["release_id"] != release_id:
            raise ValidationError("Database does not match release identity")
        if (
            stored[0]["synthetic"] != manifest["synthetic"]
            or str(stored[0]["cutoff"]) != manifest["cutoff"]
        ):
            raise ValidationError("Database release basis differs from manifest")
        validate(connection)
    return manifest


def require_no_live_replacement(root: Path, manifest: dict[str, Any]) -> None:
    """Fixtures and unreviewed cohorts never replace a stronger publication in place."""
    pointer_path = root / "current.json"
    weaker = manifest["synthetic"] or not manifest["mapping_reviewed"]
    if not weaker or not pointer_path.exists():
        return
    pointer = json.loads(pointer_path.read_bytes())
    current_path = release_path(root, pointer["release_id"]) / "manifest.json"
    current = json.loads(current_path.read_bytes())
    if manifest["synthetic"] and not current["synthetic"]:
        raise ValidationError(
            "This data directory publishes a non-synthetic release; "
            "use a separate --data-dir for synthetic examples"
        )
    if not manifest["synthetic"] and not current["synthetic"] and current["mapping_reviewed"]:
        raise ValidationError(
            "This data directory publishes a reviewed release; "
            "use a separate --data-dir for an unreviewed cohort"
        )


def publish(root: Path, release_id: str, *, allow_unreviewed: bool = False) -> dict[str, Any]:
    with publication_lock(root):
        manifest = verify_release(root, release_id)
        require_no_live_replacement(root, manifest)
        database_path = release_path(root, release_id) / "warehouse.duckdb"
        with duckdb.connect(str(database_path), read_only=True) as connection:
            feasibility_gate(connection, allow_unreviewed=allow_unreviewed)
        pointer = {
            "release_id": release_id,
            "manifest_sha256": digest((database_path.parent / "manifest.json").read_bytes()),
        }
        atomic_write(root / "current.json", json_bytes(pointer))
        return manifest


def current_release(root: Path) -> tuple[Path, dict[str, Any]]:
    pointer_path = root / "current.json"
    if not pointer_path.exists():
        raise ContractError(f"No published release in {root}; publish one or pass --release-id")
    pointer = json.loads(pointer_path.read_bytes())
    path = release_path(root, pointer["release_id"])
    if digest((path / "manifest.json").read_bytes()) != pointer["manifest_sha256"]:
        raise ValidationError("Published manifest checksum mismatch")
    manifest = verify_release(root, pointer["release_id"])
    return path, manifest


def copy_sources(archive: Archive, snapshots: Sequence[Any], destination: Path) -> None:
    from growth_cost.sec import write_bundle

    for snapshot in snapshots:
        archive.payload(snapshot)
        atomic_write(
            destination / snapshot.storage_ref, (archive.root / snapshot.storage_ref).read_bytes()
        )
    write_bundle(destination / "manifest.json", list(snapshots))
