"""Normalize individual disclosures for fact selection in SQL."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from growth_cost.contracts import (
    ContractError,
    Snapshot,
    digest,
    fits_decimal_storage,
    numeric_fact,
    validate_cik,
)


@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    form: str
    filing_date: str
    accepted_at: str | None
    report_date: str | None
    primary_document_url: str
    snapshot_id: str


@dataclass(frozen=True)
class Fact:
    observation_id: str
    snapshot_id: str
    cik: str
    accession: str
    taxonomy: str
    concept: str
    unit: str
    period_start: str | None
    period_end: str
    # None when DECIMAL(38,6) cannot hold it exactly; raw_value always keeps the disclosure.
    value: Decimal | None
    raw_value: str
    form: str
    filed: str
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None


def _accession(value: str) -> str:
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", value):
        raise ContractError("Invalid filing accession")
    return value


def normalize_filings(payload: dict[str, Any], snapshot: Snapshot) -> list[Filing]:
    if "cik" in payload and validate_cik(str(payload["cik"])) != snapshot.cik:
        raise ContractError("Submissions CIK disagrees with source manifest")
    columns = payload["filings"]["recent"] if "filings" in payload else payload
    count = len(columns["accessionNumber"])
    for key in ("accessionNumber", "form", "filingDate", "primaryDocument"):
        if len(columns[key]) != count:
            raise ContractError("Submissions columns have inconsistent lengths")
    result = []
    for index in range(count):
        accession = _accession(columns["accessionNumber"][index])
        filed = columns["filingDate"][index]
        date.fromisoformat(filed)
        accepted = columns.get("acceptanceDateTime", [None] * count)[index] or None
        if accepted:
            datetime.fromisoformat(accepted)
        report_date = columns.get("reportDate", [None] * count)[index] or None
        if report_date:
            date.fromisoformat(report_date)
        document = columns["primaryDocument"][index]
        if document.startswith("/") or ".." in document.split("/"):
            raise ContractError("Invalid primary document path")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(snapshot.cik)}/"
            f"{accession.replace('-', '')}/{quote(document, safe='/')}"
        )
        result.append(
            Filing(
                snapshot.cik,
                accession,
                columns["form"][index],
                filed,
                accepted,
                report_date,
                url,
                snapshot.snapshot_id,
            )
        )
    return result


def normalize_facts(payload: dict[str, Any], snapshot: Snapshot) -> list[Fact]:
    if validate_cik(str(payload["cik"])) != snapshot.cik:
        raise ContractError("Company Facts CIK disagrees with source manifest")
    facts = []
    for taxonomy, concepts in payload["facts"].items():
        for concept, definition in concepts.items():
            for unit, observations in definition["units"].items():
                for index, observation in enumerate(observations):
                    start = observation.get("start")
                    end = observation["end"]
                    date.fromisoformat(end)
                    date.fromisoformat(observation["filed"])
                    if start and date.fromisoformat(start) > date.fromisoformat(end):
                        raise ContractError("Duration fact ends before it starts")
                    identity = f"{snapshot.snapshot_id}/{taxonomy}/{concept}/{unit}/{index}"
                    number = numeric_fact(observation["val"])
                    facts.append(
                        Fact(
                            digest(identity.encode()),
                            snapshot.snapshot_id,
                            snapshot.cik,
                            _accession(observation["accn"]),
                            taxonomy,
                            concept,
                            unit,
                            start,
                            end,
                            number if fits_decimal_storage(number) else None,
                            format(number, "f"),
                            observation["form"],
                            observation["filed"],
                            observation.get("fy"),
                            observation.get("fp"),
                            observation.get("frame"),
                        )
                    )
    return facts
