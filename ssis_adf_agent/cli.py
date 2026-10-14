"""Customer-facing CLI for the SSIS-to-ADF migration workflow."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .workflow import (
    deploy_workspace,
    load_workspace,
    record_gate_evidence,
    record_reconciliation,
    record_review_decision,
    run_migration,
    validate_workspace,
)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ssis-adf",
        description=(
            "Deterministic-first SSIS-to-ADF migration workflow. "
            "AI is reserved for uncertain logic and mismatch diagnosis."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser(
        "migrate",
        help="Inventory, analyze, convert, and create the AI review queue.",
    )
    migrate.add_argument("--source", type=_path, required=True)
    migrate.add_argument("--workspace", type=_path, required=True)
    migrate.add_argument("--no-recursive", action="store_true")
    migrate.add_argument("--no-trigger", action="store_true")
    migrate.add_argument("--integration-runtime", default="SelfHostedIR")
    migrate.add_argument(
        "--auth-type",
        choices=["SystemAssignedManagedIdentity", "SQL", "ServicePrincipal"],
        default="SystemAssignedManagedIdentity",
    )
    migrate.add_argument("--use-key-vault", action="store_true")
    migrate.add_argument("--key-vault-url", default="https://TODO.vault.azure.net/")

    status = subparsers.add_parser("status", help="Show migration and gate status.")
    status.add_argument("--workspace", type=_path, required=True)

    review = subparsers.add_parser(
        "review",
        help="List or resolve explicit AI/review findings.",
    )
    review.add_argument("--workspace", type=_path, required=True)
    review.add_argument("--resolve")
    review.add_argument("--decision")
    review.add_argument("--approved-by")

    validate = subparsers.add_parser(
        "validate",
        help="Validate generated artifacts and enforce the G2 build gate.",
    )
    validate.add_argument("--workspace", type=_path, required=True)
    validate.add_argument("--package")

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Record reconciliation evidence and route mismatches back to AI review.",
    )
    reconcile.add_argument("--workspace", type=_path, required=True)
    reconcile.add_argument("--package", required=True)
    reconcile.add_argument("--result", choices=["pass", "fail"], required=True)
    reconcile.add_argument("--evidence", type=_path, required=True)
    reconcile.add_argument("--notes", default="")

    gate = subparsers.add_parser(
        "gate",
        help="Record assessment, test, or cutover evidence for G1, G3, or G5.",
    )
    gate.add_argument("--workspace", type=_path, required=True)
    gate.add_argument("--package", required=True)
    gate.add_argument("--gate", choices=["G1", "G3", "G5"], required=True)
    gate.add_argument("--result", choices=["pass", "fail"], required=True)
    gate.add_argument("--evidence", type=_path, required=True)
    gate.add_argument("--approved-by", required=True)
    gate.add_argument("--notes", default="")

    deploy = subparsers.add_parser(
        "deploy",
        help="Deploy a G2-approved package to ADF; triggers remain stopped.",
    )
    deploy.add_argument("--workspace", type=_path, required=True)
    deploy.add_argument("--package", required=True)
    deploy.add_argument("--subscription-id", required=True)
    deploy.add_argument("--resource-group", required=True)
    deploy.add_argument("--factory-name", required=True)
    deploy.add_argument("--dry-run", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        manifest = run_migration(
            args.source,
            args.workspace,
            recursive=not args.no_recursive,
            generate_trigger=not args.no_trigger,
            on_prem_ir_name=args.integration_runtime,
            auth_type=args.auth_type,
            use_key_vault=args.use_key_vault,
            kv_url=args.key_vault_url,
        )
        print(f"Migration workspace created: {args.workspace.resolve()}")
        print(f"Packages converted: {len(manifest['packages'])}")
        print(f"Package failures: {len(manifest['errors'])}")
        print(f"Report: {(args.workspace / 'migration_report.md').resolve()}")
        print(
            "AI queue: "
            f"{(args.workspace / '02_assessment' / 'ai_review_queue.json').resolve()}"
        )
        return 1 if manifest["errors"] else 0

    if args.command == "status":
        manifest, queue = load_workspace(args.workspace)
        _print_json({
            "migration_name": manifest["migration_name"],
            "packages": manifest["packages"],
            "open_review_items": [
                item for item in queue if item["status"] == "open"
            ],
            "errors": manifest["errors"],
        })
        return 0

    if args.command == "review":
        if not args.resolve:
            _, queue = load_workspace(args.workspace)
            _print_json([item for item in queue if item["status"] == "open"])
            return 0
        if not args.decision or not args.approved_by:
            raise ValueError("--decision and --approved-by are required with --resolve")
        item = record_review_decision(
            args.workspace,
            args.resolve,
            args.decision,
            args.approved_by,
        )
        _print_json(item)
        return 0

    if args.command == "validate":
        result = validate_workspace(args.workspace, args.package)
        _print_json(result)
        return 1 if any(
            package["status"] == "blocked" for package in result["packages"]
        ) else 0

    if args.command == "reconcile":
        record = record_reconciliation(
            args.workspace,
            args.package,
            args.result,
            args.evidence,
            args.notes,
        )
        _print_json(record)
        return 0 if args.result == "pass" else 2

    if args.command == "gate":
        record = record_gate_evidence(
            args.workspace,
            args.package,
            args.gate,
            args.result,
            args.evidence,
            args.approved_by,
            args.notes,
        )
        _print_json(record)
        return 0 if args.result == "pass" else 2

    if args.command == "deploy":
        result = deploy_workspace(
            args.workspace,
            args.package,
            subscription_id=args.subscription_id,
            resource_group=args.resource_group,
            factory_name=args.factory_name,
            dry_run=args.dry_run,
        )
        _print_json(result)
        return 1 if result["failed"] else 0

    raise ValueError(f"Unknown command: {args.command}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        sys.exit(_run(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
