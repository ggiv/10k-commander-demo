import json
import os
import subprocess
import sys
from pathlib import Path


def test_offline_cli_build_trace_and_missingness(tmp_path: Path) -> None:
    command = [sys.executable, "-m", "growth_cost.cli", "--data-dir", str(tmp_path)]
    demo = subprocess.run([*command, "demo"], check=True, capture_output=True, text=True)
    result = json.loads(demo.stdout)
    assert result["synthetic"] and result["published"]
    trace = subprocess.run(
        [
            *command,
            "inspect",
            "--cik",
            "9000000001",
            "--period-end",
            "2025-12-31",
            "--metric",
            "free_cash_flow_margin",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    inputs = json.loads(trace.stdout)["inputs"]
    assert {i["role"] for i in inputs} == {
        "current_revenue",
        "operating_cash_flow",
        "cash_purchases_of_ppe",
    }
    assert all(i["metric_value"] == 30 for i in inputs)


def test_live_refresh_requires_contact_before_any_request(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("SEC_USER_AGENT", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "growth_cost.cli",
            "--data-dir",
            str(tmp_path),
            "refresh",
            "--companies",
            "tests/fixtures/companies.csv",
            "--mappings",
            "tests/fixtures/concepts.yml",
            "--cutoff",
            "2026-11-04",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "contact email" in result.stderr
    assert not (tmp_path / "current.json").exists()
