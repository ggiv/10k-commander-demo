"""Generate explicitly synthetic SEC-shaped examples, never empirical company data.

Run with `uv run python tests/fixtures/generate.py` when intentionally editing fixtures.
Expected financial results are maintained separately in expectations.json.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

from growth_cost.contracts import json_bytes
from growth_cost.sec import Archive, atomic_write, write_bundle

ROOT = Path(__file__).parent
CONCEPTS = (
    "Revenues",
    "OperatingIncomeLoss",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "ShareBasedCompensation",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)
# Independent input examples, in millions (shares are counts, not dollars).
SERIES = (
    (
        [80, 100, 120, 140, 160, 200],
        [4, 10, 18, 28, 40, 60],
        [12, 20, 30, 38, 48, 70],
        [2, 4, 6, 8, 8, 10],
        [8, 10, 12, 14, 16, 20],
        [100, 102, 104, 106, 108, 110],
    ),
    (
        [100, 200, 400, 450, 500, 600],
        [-20, -15, -10, 0, 10, 30],
        [10, 30, 70, 80, 100, 120],
        [5, 10, 15, 20, 25, 30],
        [30, 50, 80, 85, 90, 100],
        [50, 55, 60, 65, 70, 75],
    ),
    (
        [50, 80, 120, 180, 240, 300],
        [-40, -50, -60, -70, -60, -50],
        [-10, 0, 10, 30, 50, 80],
        [5, 7, 10, 15, 20, 25],
        [20, 35, 50, 70, 85, 100],
        [60, 70, 80, 90, 100, 220],
    ),
)


def main() -> None:
    archive = Archive(ROOT / "bundle")
    snapshots = []
    for company_index, series in enumerate(SERIES):
        cik = str(9000000001 + company_index)
        facts: dict[str, Any] = {
            "cik": int(cik),
            "entityName": "SYNTHETIC FIXTURE",
            "facts": {"us-gaap": {}},
        }
        concepts = facts["facts"]["us-gaap"]
        for concept in CONCEPTS:
            unit = "shares" if concept == CONCEPTS[-1] else "USD"
            concepts[concept] = {"label": concept, "units": {unit: []}}
        columns: dict[str, list[Any]] = {
            key: []
            for key in (
                "accessionNumber",
                "filingDate",
                "acceptanceDateTime",
                "reportDate",
                "form",
                "primaryDocument",
            )
        }
        for offset, year in enumerate(range(2020, 2026)):
            end = f"{year}-12-31" if company_index < 2 else f"{year + 1}-01-31"
            start = f"{year}-01-01" if company_index < 2 else f"{year}-02-01"
            if company_index == 2 and year == 2025:
                start = "2025-02-03"
            accession = f"{cik}-{str(year + 1)[2:]}-000001"
            filed = f"{year + 1}-03-15"
            for key, value in zip(
                columns,
                (
                    accession,
                    filed,
                    f"{filed}T12:00:00Z",
                    end,
                    "10-K",
                    "synthetic-10k.htm",
                ),
                strict=True,
            ):
                columns[key].append(value)
            for measure, concept in enumerate(CONCEPTS):
                if company_index == 1 and year == 2025 and measure == 3:
                    continue  # Missing cash investment is not zero.
                value = series[measure][offset] * 1_000_000
                if company_index == 0 and year == 2024 and measure == 0:
                    value = 150_000_000  # Later comparative changes it to 160m.
                observation = {
                    "start": start,
                    "end": end,
                    "val": value,
                    "accn": accession,
                    "fy": year,
                    "fp": "FY",
                    "form": "10-K",
                    "filed": filed,
                }
                unit = "shares" if measure == 5 else "USD"
                concepts[concept]["units"][unit].append(observation)
        if company_index == 0:
            revenue = concepts[CONCEPTS[0]]["units"]["USD"]
            restatement = copy.deepcopy(revenue[-2])
            restatement.update(
                val=160_000_000, accn=columns["accessionNumber"][-1], filed="2026-03-15"
            )
            revenue.extend([restatement, copy.deepcopy(restatement)])
            future = copy.deepcopy(revenue[-1])
            future.update(val=99_000_000_000, accn=f"{cik}-27-000001", filed="2027-03-15")
            revenue.append(future)
            # Future filings remain captured, but cannot win this release.
            for key, value in zip(
                columns,
                (
                    f"{cik}-27-000001",
                    "2027-03-15",
                    "2027-03-15T12:00:00Z",
                    "2026-12-31",
                    "10-K",
                    "synthetic-future.htm",
                ),
                strict=True,
            ):
                columns[key].append(value)
        if company_index == 1:
            income = concepts[CONCEPTS[1]]["units"]["USD"]
            conflict = copy.deepcopy(income[-1])
            conflict["val"] = 31_000_000
            income.append(conflict)
            wrong_unit = copy.deepcopy(concepts[CONCEPTS[0]]["units"]["USD"][-1])
            wrong_unit["val"] = 999_000_000
            concepts[CONCEPTS[0]]["units"]["EUR"] = [wrong_unit]
            shares = concepts[CONCEPTS[-1]]["units"]["shares"]
            instant = copy.deepcopy(shares[-1])
            instant.pop("start")
            instant["val"] = 900_000_000
            shares.append(instant)
        submissions = {"cik": cik, "filings": {"recent": columns, "files": []}}
        for kind, payload in (("submissions", submissions), ("companyfacts", facts)):
            snapshot = archive.capture(
                json_bytes(payload),
                url=f"https://example.invalid/sec/{cik}/{kind}.json",
                kind=kind,
                cik=cik,
                run_id="synthetic-fixture-v1",
            )
            snapshots.append(replace(snapshot, retrieved_at="2026-11-04T00:00:00+00:00"))
    write_bundle(ROOT / "bundle" / "manifest.json", snapshots, synthetic=True)
    atomic_write(
        ROOT / "README.md",
        (
            b"# Synthetic reproduction fixture\n\n"
            b"All companies, disclosures, accessions and filing URLs in this fixture "
            b"are fictional.\n"
            b"These are SEC-shaped engineering examples, not source reconciliations "
            b"or empirical findings.\n"
            b"The input arrays live in generate.py; hand-worked expectations live "
            b"in expectations.json.\n"
            b"Example A latest: revenue 200m; prior restated revenue 160m; operating income 60m;\n"
            b"OCF 70m; PP&E cash purchases 10m; SBC 20m; basic shares 110m vs 108m.\n"
            b"Thus growth 25%; operating margin 30%; FCF margin 30%; SBC intensity 10%;\n"
            b"basic share growth 1.85185185185185%; operating margin change 5pp.\n"
            b"Example B tests ambiguity, missing investment, wrong units and instant-vs-duration.\n"
            b"Example C tests a reporting gap, unreconciled share split and investment scope.\n"
        ),
    )


if __name__ == "__main__":
    main()
