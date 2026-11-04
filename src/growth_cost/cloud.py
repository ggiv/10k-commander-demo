"""Explicit MotherDuck operations; local publication never depends on a cloud write.

Each cloud database is an immutable release destination. A publication receipt is
written only after comparison and validation, so a failed upload cannot replace a
previous release. Consumers must check the receipt before reading a new database.
"""

from __future__ import annotations

import os
import platform
import re
import statistics
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb

from growth_cost.contracts import ContractError, digest, json_bytes
from growth_cost.sec import atomic_write
from growth_cost.warehouse import feasibility_gate, release_path, rows, validate, verify_release


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", value):
        raise ContractError("Cloud identifiers must be lowercase ASCII letters/digits/underscores")
    return f'"{value}"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(token_name: str = "motherduck_token") -> duckdb.DuckDBPyConnection:
    token = os.environ.get(token_name)
    if not token:
        raise ContractError(f"Set {token_name} for this authenticated command")
    # The URI stays in process memory. CLI cloud errors suppress driver messages,
    # which can include connection details. Never log this URI.
    return duckdb.connect(f"md:?motherduck_token={quote(token, safe='')}")


@contextmanager
def cloud_connection(token_name: str = "motherduck_token") -> Any:
    connection = connect(token_name)
    try:
        connection.execute("SET TimeZone = 'UTC'")
        yield connection
    finally:
        connection.close()


def result_hash(data: list[dict[str, Any]]) -> str:
    return digest(json_bytes(sorted(json_bytes(row).decode() for row in data)))


def publish_cloud(
    root: Path,
    release_id: str,
    prefix: str = "growth_cost",
    *,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    manifest = verify_release(root, release_id)
    local = release_path(root, release_id) / "warehouse.duckdb"
    database = f"{prefix}_{release_id}"
    quoted = identifier(database)
    with duckdb.connect(str(local), read_only=True) as local_connection:
        local_connection.execute("SET TimeZone = 'UTC'")
        feasibility_gate(local_connection, allow_unreviewed=allow_unreviewed)
        tables = [row[0] for row in local_connection.execute("SHOW TABLES").fetchall()]
        expected = {
            name: result_hash(rows(local_connection, f"SELECT * FROM {identifier(name)}"))
            for name in tables
        }
    started = time.monotonic()
    with cloud_connection() as connection:
        connection.execute(f"CREATE DATABASE IF NOT EXISTS {quoted}")
        connection.execute(f"USE {quoted}")
        connection.execute(f"ATTACH {literal(str(local.resolve()))} AS local_release (READ_ONLY)")
        for table in tables:
            name = identifier(table)
            # Resumption may fill missing tables, but never overwrites existing data.
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {quoted}.main.{name} AS "
                f"SELECT * FROM local_release.main.{name}"
            )
            actual = rows(connection, f"SELECT * FROM {quoted}.main.{name}")
            if result_hash(actual) != expected[table]:
                raise ContractError(f"Cloud table differs from immutable release: {table}")
        validate(connection)
        receipt = {
            "release_id": release_id,
            "database": database,
            "source_manifest_hash": manifest["source_manifest_hash"],
            "verified_tables": len(tables),
            "synthetic": manifest["synthetic"],
            "mapping_reviewed": manifest["mapping_reviewed"],
            "duckdb_version": duckdb.__version__,
            "duration_seconds": time.monotonic() - started,
            "sharing_verified": False,
        }
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cloud_publication (release_id VARCHAR PRIMARY KEY, "
            "source_manifest_hash VARCHAR, verified_at TIMESTAMPTZ)"
        )
        connection.execute(
            "INSERT INTO cloud_publication VALUES (?, ?, current_timestamp) ON CONFLICT DO NOTHING",
            [release_id, manifest["source_manifest_hash"]],
        )
    atomic_write(root / "cloud" / f"{release_id}.json", json_bytes(receipt))
    return receipt


def hybrid_query(database: str) -> str:
    return f"""
        SELECT m.release_id, c.experimental_group, m.metric,
               count(m.value) AS observations, avg(m.value) AS mean_value
        FROM {identifier(database)}.main.metrics m
        JOIN local_experiment.main.cohort c USING (cik)
        GROUP BY m.release_id, c.experimental_group, m.metric
        ORDER BY c.experimental_group, m.metric
    """


def load_local_cohort(connection: duckdb.DuckDBPyConnection, cohort: Path) -> None:
    connection.execute("ATTACH ':memory:' AS local_experiment")
    connection.execute(
        "CREATE TABLE local_experiment.main.cohort AS SELECT "
        "lpad(cik, 10, '0') AS cik, experimental_group "
        f"FROM read_csv({literal(str(cohort.resolve()))}, all_varchar = true)"
    )
    duplicate = connection.execute(
        "SELECT count(*) FROM (SELECT cik FROM local_experiment.main.cohort "
        "GROUP BY cik HAVING count(*) > 1)"
    ).fetchone()
    if duplicate and duplicate[0]:
        raise ContractError("Alternative cohort must contain one classification per CIK")


def benchmark(
    root: Path,
    release_id: str,
    database: str,
    cohort: Path,
    repeats: int = 5,
) -> dict[str, Any]:
    verify_release(root, release_id)
    if not 3 <= repeats <= 20:
        raise ContractError("Use 3–20 repetitions")
    local_path = release_path(root, release_id) / "warehouse.duckdb"
    results: dict[str, Any] = {}
    reference = None
    with duckdb.connect() as local, cloud_connection() as cloud:
        local.execute(
            f"ATTACH {literal(str(local_path.resolve()))} AS {identifier(database)} (READ_ONLY)"
        )
        receipt = rows(cloud, f"SELECT * FROM {identifier(database)}.main.cloud_publication")
        if len(receipt) != 1 or receipt[0]["release_id"] != release_id:
            raise ContractError("Cloud publication receipt does not match the local release")
        for name, connection in (("local", local), ("hybrid", cloud)):
            load_local_cohort(connection, cohort)
            query = hybrid_query(database)
            plan = connection.execute("EXPLAIN " + query).fetchall()
            timings = []
            data = []
            for _ in range(repeats):
                started = time.perf_counter()
                data = rows(connection, query)
                timings.append(time.perf_counter() - started)
                if not data:
                    raise ContractError(
                        "Alternative cohort matched no metrics; benchmark is invalid"
                    )
                actual = result_hash(data)
                if reference is None:
                    reference = actual
                if actual != reference:
                    raise ContractError("Benchmark result equivalence failed")
            results[name] = {
                "latency_seconds": timings,
                "median_seconds": statistics.median(timings),
                "returned_rows": len(data),
                "serialized_result_bytes": len(json_bytes(data)),
                "result_hash": reference,
                "explain": plan,
            }
    return {
        "release_id": release_id,
        "duckdb_version": duckdb.__version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "local_database_bytes": local_path.stat().st_size,
        "repeats": repeats,
        "cache_conditions": "Uncontrolled initial cache; repeated queries; no cache flush",
        "transfer_measurement": "Serialized returned JSON bytes only; not network bytes",
        "usage_cost": None,
        "usage_cost_note": "No billing usage records supplied",
        "measurements": results,
    }


def check_reader(share_url: str, expected_release_id: str) -> dict[str, Any]:
    if not share_url.startswith("md:_share/"):
        raise ContractError("Expected a MotherDuck share URI")
    writer = os.environ.get("motherduck_token")
    reader = os.environ.get("MOTHERDUCK_READER_TOKEN")
    if not reader or reader == writer:
        raise ContractError("Reader verification requires a distinct MOTHERDUCK_READER_TOKEN")
    with cloud_connection("MOTHERDUCK_READER_TOKEN") as connection:
        connection.execute(f"ATTACH {literal(share_url)} AS release_reader")
        connection.execute("REFRESH DATABASE release_reader")
        actual = rows(connection, "SELECT release_id FROM release_reader.main.cloud_publication")
        if actual != [{"release_id": expected_release_id}]:
            raise ContractError("Reader sees a different or unvalidated release")
        denied = False
        try:
            # No records are written: a zero-row DELETE tests write permissions.
            connection.execute("DELETE FROM release_reader.main.metrics WHERE false")
        except duckdb.Error as error:
            message = str(error).lower()
            if any(
                word in message for word in ("read-only", "read only", "permission", "not allowed")
            ):
                denied = True
            else:
                raise
        if not denied:
            raise ContractError("Reader share unexpectedly permits writes")
        count = connection.execute("SELECT count(*) FROM release_reader.main.metrics").fetchone()
    return {
        "release_id": expected_release_id,
        "read_only_verified": True,
        "refresh_verified": True,
        "metric_rows": count[0] if count else 0,
    }
