"""Command-line interface for building, inspecting, and publishing releases."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from growth_cost import analysis, cloud
from growth_cost.contracts import ContractError, json_bytes, validate_cik
from growth_cost.pipeline import build, refresh
from growth_cost.warehouse import publish, verify_release


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Auditable annual SEC metrics; no investment ranking"
    )
    result.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("GROWTH_COST_DATA_DIR", "data"))
    )
    commands = result.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Build and publish the synthetic offline example")
    demo.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"))
    demo.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 11, 4))
    candidate = commands.add_parser("build", help="Build a candidate; never changes publication")
    candidate.add_argument("--bundle", type=Path, required=True)
    candidate.add_argument("--companies", type=Path, required=True)
    candidate.add_argument("--mappings", type=Path, required=True)
    candidate.add_argument("--reconciliation", type=Path)
    candidate.add_argument("--cutoff", type=date.fromisoformat, required=True)
    live = commands.add_parser(
        "refresh", help="Retrieve SEC sources and build an unpublished candidate"
    )
    live.add_argument("--companies", type=Path, required=True)
    live.add_argument("--mappings", type=Path, required=True)
    live.add_argument("--reconciliation", type=Path)
    live.add_argument("--cutoff", type=date.fromisoformat, required=True)
    unreviewed_help = "Publish unreviewed mappings as visibly unreviewed; skips reconciliation"
    published = commands.add_parser("publish")
    published.add_argument("release_id")
    published.add_argument("--unreviewed", action="store_true", help=unreviewed_help)
    commands.add_parser("validate").add_argument("release_id")
    inspect = commands.add_parser(
        "inspect", help="Trace a published or candidate metric to all input evidence"
    )
    inspect.add_argument("--release-id")
    inspect.add_argument("--cik", type=validate_cik, required=True)
    inspect.add_argument("--period-end", type=date.fromisoformat, required=True)
    inspect.add_argument("--metric", required=True)
    report = commands.add_parser(
        "report", help="Read published data, including missing observations"
    )
    report.add_argument(
        "kind",
        choices=[
            "overview",
            "coverage",
            "exceptions",
            "trend_summaries",
            "metric_definitions",
            "annotations",
            "reconciliation_inputs",
            "mapping_audit",
        ],
    )
    report.add_argument("--release-id")
    report.add_argument("--window-start", type=date.fromisoformat)
    report.add_argument("--window-end", type=date.fromisoformat)
    report.add_argument("--business-model")
    for name in ("export", "bundle"):
        sub = commands.add_parser(name)
        sub.add_argument("destination", type=Path)
    commands.add_parser("status")
    remote = commands.add_parser(
        "cloud-publish", help="Upload a validated immutable release to MotherDuck"
    )
    remote.add_argument("release_id")
    remote.add_argument("--prefix", default="growth_cost")
    remote.add_argument("--unreviewed", action="store_true", help=unreviewed_help)
    benchmark = commands.add_parser("benchmark", help="Compare fixed local and hybrid queries")
    benchmark.add_argument("release_id")
    benchmark.add_argument("--database", required=True)
    benchmark.add_argument("--cohort", type=Path, required=True)
    benchmark.add_argument("--repeats", type=int, default=5)
    reader = commands.add_parser(
        "cloud-check-reader", help="Verify a shared release with reader credentials"
    )
    reader.add_argument("release_id")
    return result


def execute(args: argparse.Namespace) -> dict[str, Any]:
    root = args.data_dir.resolve()
    match args.command:
        case "demo":
            fixtures = args.fixtures
            manifest = build(
                root,
                fixtures / "bundle/manifest.json",
                fixtures / "companies.csv",
                fixtures / "concepts.yml",
                args.cutoff,
            )
            publish(root, manifest["release_id"])
            return {
                "release_id": manifest["release_id"],
                "synthetic": manifest["synthetic"],
                "published": True,
                "counts": manifest["counts"],
                "validation": manifest["validation"],
                "cloud_verified": False,
            }
        case "build" | "refresh":
            bundle = (
                args.bundle
                if args.command == "build"
                else refresh(
                    root,
                    args.companies,
                    args.mappings,
                    os.environ.get("SEC_USER_AGENT", ""),
                )
            )
            manifest = build(
                root, bundle, args.companies, args.mappings, args.cutoff, args.reconciliation
            )
            return {
                "release_id": manifest["release_id"],
                "synthetic": manifest["synthetic"],
                "published": False,
                "counts": manifest["counts"],
                "validation": manifest["validation"],
            }
        case "publish":
            manifest = publish(root, args.release_id, allow_unreviewed=args.unreviewed)
            return {
                "release_id": manifest["release_id"],
                "published": True,
                "synthetic": manifest["synthetic"],
                "mapping_reviewed": manifest["mapping_reviewed"],
            }
        case "validate":
            manifest = verify_release(root, args.release_id)
            return {"release_id": args.release_id, "validation": manifest["validation"]}
        case "status":
            return analysis.stored_manifest(root)
        case "report":
            return analysis.report(
                root,
                args.kind,
                release_id=args.release_id,
                window_start=args.window_start,
                window_end=args.window_end,
                business_model=args.business_model,
            )
        case "inspect":
            return analysis.inspect_metric(
                root, args.cik, args.period_end, args.metric, release_id=args.release_id
            )
        case "export":
            return analysis.export_release(root, args.destination)
        case "bundle":
            return analysis.reproduction_bundle(root, args.destination)
        case "cloud-publish":
            return cloud.publish_cloud(
                root, args.release_id, args.prefix, allow_unreviewed=args.unreviewed
            )
        case "benchmark":
            return cloud.benchmark(root, args.release_id, args.database, args.cohort, args.repeats)
        case "cloud-check-reader":
            return cloud.check_reader(os.environ.get("MOTHERDUCK_SHARE_URL", ""), args.release_id)
        case _:
            raise ContractError("Unknown command")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    args = parser().parse_args()
    try:
        result = execute(args)
    except (ContractError, OSError, ValueError, duckdb.Error) as error:
        # Driver errors may include remote connection strings, so their text is never shown.
        # Application errors provide details about invalid inputs and publication failures.
        if isinstance(error, duckdb.Error):
            if args.command in {"cloud-publish", "benchmark", "cloud-check-reader"}:
                message = (
                    f"Authenticated operation failed ({type(error).__name__}); credentials omitted"
                )
            else:
                message = (
                    f"Database operation failed ({type(error).__name__}); publication unchanged"
                )
        else:
            message = str(error)
        print(json_bytes({"error": message}).decode(), file=sys.stderr, end="")
        return 1
    print(json_bytes(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
