"""Explicit opt-in: creates one isolated cloud DB and drops only that DB afterward."""

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from growth_cost.cloud import benchmark, cloud_connection, identifier, publish_cloud


@pytest.mark.cloud
@pytest.mark.skipif(
    os.environ.get("GROWTH_COST_CLOUD_TEST") != "1",
    reason="Set GROWTH_COST_CLOUD_TEST=1 and motherduck_token in a trusted environment",
)
def test_real_cloud_upload_equivalence_and_dual_execution(
    example: tuple[Path, dict[str, Any]],
    tmp_path: Path,
) -> None:
    root, manifest = example
    prefix = f"gc_test_{uuid.uuid4().hex[:10]}"
    database = f"{prefix}_{manifest['release_id']}"
    cohort = tmp_path / "alternative.csv"
    cohort.write_text(
        "cik,experimental_group\n9000000001,subscription\n9000000002,subscription\n9000000003,consumption\n"
    )
    try:
        receipt = publish_cloud(root, manifest["release_id"], prefix)
        assert receipt["verified_tables"] > 10
        results = benchmark(root, manifest["release_id"], database, cohort, repeats=3)
        assert (
            results["measurements"]["local"]["result_hash"]
            == results["measurements"]["hybrid"]["result_hash"]
        )
        assert results["measurements"]["hybrid"]["returned_rows"] > 0
    finally:
        with cloud_connection() as con:
            con.execute(f"DROP DATABASE IF EXISTS {identifier(database)}")
