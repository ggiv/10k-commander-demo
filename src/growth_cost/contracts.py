"""Data types and validation for source payloads and configuration."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import yaml

CANONICAL = (
    "revenue",
    "operating_income",
    "operating_cash_flow",
    "capex",
    "stock_compensation",
    "basic_shares",
)


class ContractError(ValueError):
    """Invalid source payload or configuration data."""


@dataclass(frozen=True)
class Company:
    cik: str
    ticker: str
    name: str
    business_model: str
    included: bool
    rationale: str


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    url: str
    retrieved_at: str
    content_hash: str
    storage_ref: str
    run_id: str
    kind: str
    cik: str


@dataclass(frozen=True)
class Mapping:
    canonical: str
    taxonomy: str
    concept: str
    unit: str
    priority: int
    cik: str = "*"
    valid_from: str = "1900-01-01"
    valid_to: str = "9999-12-31"
    multiplier: int = 1
    source_url: str = ""
    explanation: str = ""


@dataclass(frozen=True)
class Annotation:
    cik: str
    period_start: str
    period_end: str
    flag: str
    explanation: str
    source_url: str


@dataclass(frozen=True)
class Configuration:
    companies: list[Company]
    mappings: list[Mapping]
    annotations: list[Annotation]
    mapping_version: str
    reviewed: bool


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_bytes(value: Any) -> bytes:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat() if item.tzinfo else item.isoformat()
        if isinstance(item, (date, Decimal, Path)):
            return str(item)
        raise TypeError(f"Unsupported JSON boundary type: {type(item).__name__}")

    return (json.dumps(value, sort_keys=True, indent=2, default=encode) + "\n").encode()


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes(), parse_float=Decimal)


def validate_cik(value: str) -> str:
    if not value.isdigit() or len(value) > 10 or int(value) == 0:
        raise ContractError("CIK must contain one to ten digits and be nonzero")
    return value.zfill(10)


def numeric_fact(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ContractError("Facts must contain JSON numbers, not strings or binary floats")
    number = Decimal(value)
    if not number.is_finite():
        raise ContractError("Facts must be finite numbers")
    return number


def fits_decimal_storage(number: Decimal) -> bool:
    """Whether DECIMAL(38,6) holds the number exactly, without rounding."""
    if number.copy_abs() >= Decimal(10) ** 32:
        return False
    with localcontext() as context:
        context.prec = 50
        return number == number.quantize(Decimal("0.000001"))


def financial_decimal(value: Any) -> Decimal:
    number = numeric_fact(value)
    if not fits_decimal_storage(number):
        raise ContractError("Value does not fit DECIMAL(38,6) exactly; review before loading")
    return number


def load_configuration(companies_path: Path, mappings_path: Path) -> Configuration:
    with companies_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    companies = []
    for row in rows:
        if row["included"].lower() not in {"true", "false"}:
            raise ContractError("included must be true or false")
        companies.append(
            Company(
                validate_cik(row["cik"]),
                row["ticker"],
                row["name"],
                row["business_model"],
                row["included"].lower() == "true",
                row["rationale"],
            )
        )
    if len({c.cik for c in companies}) != len(companies):
        raise ContractError("Duplicate company CIK")
    if not any(c.included for c in companies):
        raise ContractError("Cohort has no included companies")
    data = yaml.safe_load(mappings_path.read_text())
    mappings = [Mapping(**row) for row in data["mappings"]]
    for mapping in mappings:
        if mapping.canonical not in CANONICAL or mapping.multiplier not in {-1, 1}:
            raise ContractError("Unknown canonical concept or invalid sign multiplier")
        expected = "shares" if mapping.canonical == "basic_shares" else "USD"
        if mapping.unit != expected or mapping.priority < 0:
            raise ContractError("Canonical unit or mapping priority is invalid")
        if mapping.cik != "*":
            validate_cik(mapping.cik)
            if len(mapping.cik) != 10:
                raise ContractError("Scoped mapping CIK must be zero padded")
        if date.fromisoformat(mapping.valid_from) > date.fromisoformat(mapping.valid_to):
            raise ContractError("Mapping date range is reversed")
        scoped = mapping.cik != "*" or mapping.multiplier != 1
        dated = mapping.valid_from != "1900-01-01" or mapping.valid_to != "9999-12-31"
        if (scoped or dated) and not (mapping.source_url and mapping.explanation):
            raise ContractError("Overrides require a source and explanation")
    annotations = [Annotation(**row) for row in data.get("annotations", [])]
    for annotation in annotations:
        validate_cik(annotation.cik)
        date.fromisoformat(annotation.period_start)
        date.fromisoformat(annotation.period_end)
        if not annotation.source_url or not annotation.explanation:
            raise ContractError("Annotations require evidence and an explanation")
    if not isinstance(data.get("reviewed"), bool):
        raise ContractError("reviewed must be a YAML boolean")
    return Configuration(companies, mappings, annotations, str(data["version"]), data["reviewed"])


def configuration_dict(config: Configuration) -> dict[str, Any]:
    return asdict(config)
