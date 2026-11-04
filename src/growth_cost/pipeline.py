"""SEC retrieval and release build orchestration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from growth_cost.contracts import (
    ContractError,
    Snapshot,
    configuration_dict,
    digest,
    json_bytes,
    load_configuration,
)
from growth_cost.normalize import Fact, Filing, normalize_facts, normalize_filings
from growth_cost.sec import (
    Archive,
    SECClient,
    atomic_write,
    read_bundle,
    retrieve_company,
    write_bundle,
)
from growth_cost.warehouse import (
    DEFINITION_VERSION,
    copy_sources,
    load_database,
    publication_lock,
    release_path,
    rows,
    validate,
    verify_release,
)

LOGGER = logging.getLogger("growth_cost")


def event(name: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": name, **fields}, default=str, sort_keys=True))


def code_identity() -> tuple[str, str]:
    package = Path(__file__).parent
    paths = sorted(p for p in package.rglob("*") if p.suffix in {".py", ".sql"})
    identity = {str(p.relative_to(package)): digest(p.read_bytes()) for p in paths}
    for name in ("pyproject.toml", "uv.lock"):
        dependency_path = package.parent.parent / name
        if dependency_path.exists():
            identity[name] = digest(dependency_path.read_bytes())
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=package,
    )
    return revision.stdout.strip() if revision.returncode == 0 else "uncommitted", digest(
        json_bytes(identity)
    )


def refresh(root: Path, companies_path: Path, mappings_path: Path, user_agent: str) -> Path:
    config = load_configuration(companies_path, mappings_path)
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    archive = Archive(root / "archive")
    snapshots = []
    previous_hashes: set[str] = set()
    latest = archive.root / "manifest.json"
    if latest.exists():
        _, previous, _ = read_bundle(latest)
        previous_hashes = {s.snapshot_id for s in previous}
    changed = []
    try:
        with SECClient(user_agent, root / "http-cache") as client:
            for company in config.companies:
                if company.included:
                    captures = retrieve_company(client, archive, company.cik, run_id)
                    snapshots.extend(captures)
                    if any(s.snapshot_id not in previous_hashes for s in captures):
                        changed.append(company.cik)
        # An interrupted retrieval leaves the last complete inventory intact.
        write_bundle(latest, snapshots)
        atomic_write(archive.root / f"run-{run_id}.json", latest.read_bytes())
        event(
            "refresh_complete",
            run_id=run_id,
            snapshots=len(snapshots),
            changed_companies=changed,
            duration_seconds=time.monotonic() - started,
        )
        return latest
    except Exception as error:
        event(
            "refresh_failed",
            run_id=run_id,
            error_type=type(error).__name__,
            duration_seconds=time.monotonic() - started,
        )
        raise


def parse_sources(
    archive: Archive, snapshots: Sequence[Snapshot]
) -> tuple[list[Filing], list[Fact]]:
    inventory: dict[tuple[str, str], Filing] = {}
    facts: list[Fact] = []
    for snapshot in snapshots:
        payload = archive.payload(snapshot)
        if snapshot.kind in {"submissions", "submissions_history"}:
            for filing in normalize_filings(payload, snapshot):
                key = (filing.cik, filing.accession)
                existing = inventory.get(key)
                # History files can overlap; only source identity may differ.
                if existing and replace(existing, snapshot_id=filing.snapshot_id) != filing:
                    raise ContractError("Conflicting filing inventory rows")
                if not existing:
                    inventory[key] = filing
        elif snapshot.kind == "companyfacts":
            facts.extend(normalize_facts(payload, snapshot))
        else:
            raise ContractError("Unknown source snapshot kind")
    return list(inventory.values()), facts


def effective_cutoff(filings: Sequence[Filing], facts: Sequence[Fact], requested: date) -> date:
    """Latest source date within the cutoff: moving past no new date cannot change results."""
    dates = {date.fromisoformat(filing.filing_date) for filing in filings}
    for fact in facts:
        dates.update((date.fromisoformat(fact.filed), date.fromisoformat(fact.period_end)))
    within = [day for day in dates if day <= requested]
    if not within:
        raise ContractError("No source dates on or before cutoff")
    return max(within)


def build(
    root: Path,
    bundle: Path,
    companies_path: Path,
    mappings_path: Path,
    requested_cutoff: date,
    reconciliation: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    config = load_configuration(companies_path, mappings_path)
    archive, snapshots, synthetic = read_bundle(bundle)
    included = {c.cik for c in config.companies if c.included}
    snapshots = [s for s in snapshots if s.cik in included]
    for cik in included:
        kinds = {s.kind for s in snapshots if s.cik == cik}
        if not {"companyfacts", "submissions"} <= kinds:
            raise ContractError(f"Source bundle incomplete for CIK {cik}")
    keys = [(s.cik, s.kind) for s in snapshots if s.kind in {"companyfacts", "submissions"}]
    if len(set(keys)) != len(keys):
        raise ContractError("Bundle contains competing snapshots for one source; choose one run")
    try:
        filings, facts = parse_sources(archive, snapshots)
        cutoff = effective_cutoff(filings, facts, requested_cutoff)
    except Exception as error:
        event(
            "build_failed",
            stage="parse",
            error_type=type(error).__name__,
            duration_seconds=time.monotonic() - started,
        )
        raise
    revision, code_hash = code_identity()
    config_hash = digest(
        json_bytes(
            {
                "configuration": configuration_dict(config),
                "reconciliation": digest(reconciliation.read_bytes()) if reconciliation else None,
            }
        )
    )
    source_manifest = sorted(
        (s.snapshot_id, s.url, s.content_hash, s.kind, s.cik) for s in snapshots
    )
    source_hash = digest(json_bytes(source_manifest))
    release_id = digest(
        json_bytes(
            {
                "cutoff": cutoff,
                "code": code_hash,
                "config": config_hash,
                "sources": source_hash,
                "synthetic": synthetic,
                "definitions": DEFINITION_VERSION,
            }
        )
    )[:24]
    target = release_path(root, release_id)
    if target.exists():
        manifest = verify_release(root, release_id)
        event("build_unchanged", release_id=release_id)
        return manifest
    staging = root / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    working = Path(tempfile.mkdtemp(prefix=f"{release_id}-", dir=staging))
    try:
        created_at = datetime.now(UTC).isoformat()
        with duckdb.connect(str(working / "warehouse.duckdb")) as connection:
            load_database(
                connection,
                config,
                snapshots,
                filings,
                facts,
                [
                    release_id,
                    cutoff,
                    config.mapping_version,
                    DEFINITION_VERSION,
                    synthetic,
                    config.reviewed,
                    revision,
                    code_hash,
                    config_hash,
                    source_hash,
                    created_at,
                ],
                reconciliation,
            )
            validation = validate(connection)
            coverage = rows(connection, "SELECT * FROM coverage ORDER BY cik, metric")
            exceptions = rows(
                connection, "SELECT * FROM exceptions ORDER BY cik, period_end, canonical"
            )
            counts = {
                name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # type: ignore[index]
                for name in (
                    "fact_candidates",
                    "selected_facts",
                    "metrics",
                    "metric_inputs",
                    "filings",
                )
            }
            connection.execute("CHECKPOINT")
        copy_sources(archive, snapshots, working / "sources")
        write_bundle(working / "sources" / "manifest.json", snapshots, synthetic=synthetic)
        atomic_write(working / "config" / "companies.csv", companies_path.read_bytes())
        atomic_write(working / "config" / "concepts.yml", mappings_path.read_bytes())
        if reconciliation:
            atomic_write(working / "config" / "reconciliation.csv", reconciliation.read_bytes())
        for name, content in (
            ("coverage", coverage),
            ("exceptions", exceptions),
            ("validation", validation),
        ):
            atomic_write(working / f"{name}.json", json_bytes(content))
        artifacts = {
            str(p.relative_to(working)): digest(p.read_bytes())
            for p in sorted(working.rglob("*"))
            if p.is_file()
        }
        manifest = {
            "format_version": 1,
            "release_id": release_id,
            "cutoff": str(cutoff),
            "requested_cutoff": str(requested_cutoff),
            "basis": "Latest eligible annual disclosures by cutoff; not point-in-time history",
            "synthetic": synthetic,
            "mapping_reviewed": config.reviewed,
            "mapping_version": config.mapping_version,
            "definition_version": DEFINITION_VERSION,
            "code_revision": revision,
            "code_hash": code_hash,
            "config_hash": config_hash,
            "source_manifest_hash": source_hash,
            "created_at": created_at,
            "counts": counts,
            "validation": validation,
            "artifacts": artifacts,
            "duration_seconds": time.monotonic() - started,
            "cloud_verified": False,
        }
        atomic_write(working / "manifest.json", json_bytes(manifest))
        with publication_lock(root):
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.replace(working, target)
        event(
            "build_complete",
            release_id=release_id,
            source_count=len(snapshots),
            **counts,
            exceptions=len(exceptions),
            validation=validation,
            duration_seconds=time.monotonic() - started,
        )
        return verify_release(root, release_id)
    except Exception as error:
        event(
            "build_failed",
            release_id=release_id,
            error_type=type(error).__name__,
            duration_seconds=time.monotonic() - started,
        )
        raise
    finally:
        shutil.rmtree(working, ignore_errors=True)
