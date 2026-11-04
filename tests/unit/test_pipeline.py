import subprocess
from pathlib import Path
from typing import Any

import duckdb
import pytest

import growth_cost.pipeline as pipeline
from growth_cost.contracts import Mapping
from growth_cost.warehouse import insert_records


def test_code_revision_comes_from_the_package_not_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(*_: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 0, stdout="abc123\n")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    assert pipeline.code_identity()[0] == "abc123"
    assert Path(seen["cwd"]) == Path(pipeline.__file__).parent


def test_insert_records_round_trips_empty_null_and_quoted_text() -> None:
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE mappings (canonical VARCHAR, taxonomy VARCHAR, concept VARCHAR, "
        "unit VARCHAR, priority INTEGER, cik VARCHAR, valid_from DATE, valid_to DATE, "
        "multiplier INTEGER, source_url VARCHAR, explanation VARCHAR)"
    )
    record = Mapping(
        "capex", "us-gaap", 'Quoted "name", with comma\nand newline', "USD", 1, explanation=""
    )
    insert_records(connection, "mappings", [record])
    stored = connection.execute("SELECT concept, source_url, explanation FROM mappings").fetchone()
    assert stored == ('Quoted "name", with comma\nand newline', "", "")
