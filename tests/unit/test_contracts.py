from decimal import Decimal
from pathlib import Path

import pytest

from growth_cost.contracts import ContractError, financial_decimal, load_configuration


@pytest.mark.parametrize("value", [True, 1.1, "100", Decimal("NaN"), Decimal("0.0000001")])
def test_no_implicit_numeric_coercion(value: object) -> None:
    with pytest.raises(ContractError):
        financial_decimal(value)


def test_decimal_storage_does_not_silently_round() -> None:
    assert financial_decimal(Decimal("123456789012345678901234567890.123456")) == Decimal(
        "123456789012345678901234567890.123456"
    )


def test_company_override_requires_accounting_evidence(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text("""
version: bad
reviewed: false
mappings:
 - {canonical: capex, taxonomy: us-gaap, concept: Capex, unit: USD, priority: 0, multiplier: -1}
""")
    with pytest.raises(ContractError, match="source and explanation"):
        load_configuration(Path("tests/fixtures/companies.csv"), path)
