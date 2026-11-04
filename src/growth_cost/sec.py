"""SEC HTTP transport and immutable source archives."""

from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from growth_cost.contracts import ContractError, Snapshot, digest, json_bytes, validate_cik

MAX_RETRY_AFTER_SECONDS = 120


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Archive:
    def __init__(self, root: Path) -> None:
        self.root = root

    def capture(self, content: bytes, *, url: str, kind: str, cik: str, run_id: str) -> Snapshot:
        content_hash = digest(content)
        storage_ref = f"objects/{content_hash[:2]}/{content_hash}.json.gz"
        destination = self.root / storage_ref
        if destination.exists():
            if digest(gzip.decompress(destination.read_bytes())) != content_hash:
                raise ContractError("Existing archive object failed content verification")
        else:
            atomic_write(destination, gzip.compress(content, mtime=0))
        return Snapshot(
            digest(f"{url}\n{content_hash}".encode()),
            url,
            datetime.now(UTC).isoformat(),
            content_hash,
            storage_ref,
            run_id,
            kind,
            validate_cik(cik),
        )

    def payload(self, snapshot: Snapshot) -> dict[str, Any]:
        path = (self.root / snapshot.storage_ref).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ContractError("Source storage reference escapes the archive")
        content = gzip.decompress(path.read_bytes())
        if digest(content) != snapshot.content_hash:
            raise ContractError("Source hash mismatch")
        from decimal import Decimal

        payload = json.loads(content, parse_float=Decimal)
        if not isinstance(payload, dict):
            raise ContractError("SEC payload must be an object")
        return payload


def read_bundle(path: Path) -> tuple[Archive, list[Snapshot], bool]:
    manifest = json.loads(path.read_bytes())
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("synthetic"), bool):
        raise ContractError("Bundle requires format_version 1 and an explicit synthetic boolean")
    snapshots = [Snapshot(**row) for row in manifest["snapshots"]]
    if len({s.snapshot_id for s in snapshots}) != len(snapshots):
        raise ContractError("Duplicate snapshot in bundle")
    for snapshot in snapshots:
        validate_cik(snapshot.cik)
        if snapshot.snapshot_id != digest(f"{snapshot.url}\n{snapshot.content_hash}".encode()):
            raise ContractError("Snapshot identity is inconsistent")
    return Archive(path.parent), snapshots, manifest["synthetic"]


class SECClient:
    """Sequential 2 rps requests with conditional caching and bounded retries."""

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        interval: float = 0.5,
    ) -> None:
        if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent):
            raise ContractError("SEC_USER_AGENT must identify your project and real contact email")
        if len(user_agent.split()) < 2 or interval < 0.5:
            raise ContractError("Use an identified user agent and at most two requests per second")
        self.cache_dir = cache_dir
        self.interval = interval
        self.last_request = 0.0
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=30,
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> SECClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def fetch(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in {"data.sec.gov", "www.sec.gov"}:
            raise ContractError("Only HTTPS SEC source URLs are allowed")
        key = digest(url.encode())
        metadata_path = self.cache_dir / f"{key}.json"
        body_path = self.cache_dir / f"{key}.body"
        cached = json.loads(metadata_path.read_bytes()) if metadata_path.exists() else {}
        headers = {}
        if body_path.exists():
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        for attempt in range(4):
            time.sleep(max(0.0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.get(url, headers=headers)
            except httpx.TransportError:
                if attempt == 3:
                    raise ContractError("SEC transport failed after four attempts") from None
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code == 304:
                if not body_path.exists():
                    raise ContractError("SEC returned 304 without a cached response")
                body = body_path.read_bytes()
                if digest(body) != cached.get("content_hash"):
                    raise ContractError("Conditional cache failed content verification")
                return body
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise ContractError("SEC unavailable after four attempts")
                retry_after = response.headers.get("Retry-After", "")
                delay = int(retry_after) if retry_after.isdigit() else 2**attempt
                # Retrying before the requested time risks extending an SEC rate-limit block.
                if delay > MAX_RETRY_AFTER_SECONDS:
                    raise ContractError(f"SEC asked to wait {delay} seconds; rerun after that")
                time.sleep(max(delay, 1))
                continue
            if response.status_code != 200:
                raise ContractError(f"SEC rejected request (HTTP {response.status_code})")
            # Validate before caching so an HTML block page cannot become a source payload.
            if not isinstance(response.json(), dict):
                raise ContractError("SEC response is not a JSON object")
            atomic_write(body_path, response.content)
            atomic_write(
                metadata_path,
                json_bytes(
                    {
                        "etag": response.headers.get("etag"),
                        "last_modified": response.headers.get("last-modified"),
                        "content_hash": digest(response.content),
                    }
                ),
            )
            return response.content
        raise AssertionError("Unreachable retry state")


def retrieve_company(
    client: SECClient,
    archive: Archive,
    cik: str,
    run_id: str,
) -> list[Snapshot]:
    cik = validate_cik(cik)
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    content = client.fetch(submissions_url)
    snapshots = [
        archive.capture(
            content,
            url=submissions_url,
            kind="submissions",
            cik=cik,
            run_id=run_id,
        )
    ]
    inventory = json.loads(content)
    for historical in inventory["filings"].get("files", []):
        name = historical["name"]
        if not re.fullmatch(r"CIK\d{10}-submissions-\d+\.json", name):
            raise ContractError("Unexpected submissions-history filename")
        url = f"https://data.sec.gov/submissions/{name}"
        snapshots.append(
            archive.capture(
                client.fetch(url),
                url=url,
                kind="submissions_history",
                cik=cik,
                run_id=run_id,
            )
        )
    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    snapshots.append(
        archive.capture(
            client.fetch(facts_url),
            url=facts_url,
            kind="companyfacts",
            cik=cik,
            run_id=run_id,
        )
    )
    return snapshots


def write_bundle(path: Path, snapshots: list[Snapshot], *, synthetic: bool = False) -> None:
    atomic_write(
        path,
        json_bytes(
            {
                "format_version": 1,
                "synthetic": synthetic,
                "snapshots": [asdict(snapshot) for snapshot in snapshots],
            }
        ),
    )
