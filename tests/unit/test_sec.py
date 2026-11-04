import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from growth_cost.contracts import ContractError
from growth_cost.sec import Archive, SECClient, retrieve_company

AGENT = "GrowthCostTests test@example.invalid"
URL = "https://data.sec.gov/submissions/CIK9000000001.json"


def test_conditional_request_reuses_verified_body(tmp_path: Path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"cik": "9000000001"}, headers={"ETag": "v1"})
        assert request.headers["If-None-Match"] == "v1"
        return httpx.Response(304)

    with SECClient(AGENT, tmp_path, transport=httpx.MockTransport(handler)) as client:
        first = client.fetch(URL)
        assert client.fetch(URL) == first
    assert len(requests) == 2


def test_rate_limit_retries_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr("growth_cost.sec.time.sleep", waits.append)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "45"})

    with SECClient(AGENT, tmp_path, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContractError, match="four attempts"):
            client.fetch(URL)
    assert len(attempts) == 4
    assert waits.count(45) == 3


def test_long_retry_after_stops_instead_of_retrying_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("growth_cost.sec.time.sleep", lambda _: None)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "999"})

    with SECClient(AGENT, tmp_path, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContractError, match="999 seconds"):
            client.fetch(URL)
    assert len(attempts) == 1


def test_submission_history_is_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("growth_cost.sec.time.sleep", lambda _: None)
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        response: dict[str, Any] = {"facts": {}}
        if request.url.path.endswith("CIK9000000001.json") and "submissions" in request.url.path:
            response = {"filings": {"files": [{"name": "CIK9000000001-submissions-001.json"}]}}
        return httpx.Response(200, json=response)

    with SECClient(AGENT, tmp_path / "cache", transport=httpx.MockTransport(handler)) as client:
        snapshots = retrieve_company(client, Archive(tmp_path / "archive"), "9000000001", "test")
    assert [s.kind for s in snapshots] == ["submissions", "submissions_history", "companyfacts"]
    assert "/submissions/CIK9000000001-submissions-001.json" in requested


def test_archive_corruption_is_detected(tmp_path: Path) -> None:
    archive = Archive(tmp_path)
    capture = archive.capture(
        b'{"value": 100}', url=URL, kind="companyfacts", cik="9000000001", run_id="t"
    )
    import gzip

    (tmp_path / capture.storage_ref).write_bytes(gzip.compress(b'{"value": 999}'))
    with pytest.raises(ContractError, match="hash mismatch"):
        archive.payload(capture)


def test_unidentified_agent_and_non_sec_url_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="contact"):
        SECClient("anonymous", tmp_path)
    with SECClient(AGENT, tmp_path) as client:
        with pytest.raises(ContractError, match="Only HTTPS SEC"):
            client.fetch("https://example.com/arbitrary")


def test_contact_not_written_to_cache(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"safe": True})

    with SECClient(AGENT, tmp_path, transport=httpx.MockTransport(handler)) as client:
        client.fetch(URL)
    for path in tmp_path.iterdir():
        assert AGENT not in path.read_text()
        if path.suffix == ".json":
            assert "headers" not in json.loads(path.read_text())
