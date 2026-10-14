"""CLI-first migration workflow built on the existing deterministic engine."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .analyzers.cdm_pattern_detector import detect_cdm_patterns
from .analyzers.complexity_scorer import score_package
from .analyzers.dependency_graph import build_package_dependency_order
from .analyzers.esi_reuse_analyzer import analyze_esi_reuse, load_esi_config
from .analyzers.gap_analyzer import analyze_gaps
from .deployer.adf_deployer import AdfDeployer
from .generators.dataflow_generator import generate_data_flows
from .generators.dataset_generator import generate_datasets
from .generators.linked_service_generator import generate_linked_services
from .generators.pipeline_generator import generate_pipeline
from .generators.trigger_generator import generate_triggers
from .parsers.models import GapItem, SSISPackage
from .parsers.readers.local_reader import LocalReader

WORKFLOW_VERSION = "1"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "package"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _review_route(gap: GapItem) -> str:
    if gap.severity == "warning":
        return "review_required"
    if gap.severity == "info":
        return "converted"

    architecture_markers = (
        "Unknown",
        "ExecuteProcess",
        "LinkedServer",
        "OPENQUERY",
        "OPENROWSET",
        "Fuzzy",
    )
    if any(marker.lower() in gap.task_type.lower() for marker in architecture_markers):
        return "architecture_decision_required"
    return "ai_refactor_required"


def _review_item(package_key: str, index: int, gap: GapItem) -> dict[str, Any]:
    route = _review_route(gap)
    return {
        "id": f"{package_key}-GAP-{index:03d}",
        "package": package_key,
        "route": route,
        "status": "open" if route != "converted" else "not_required",
        "task_id": gap.task_id,
        "task_name": gap.task_name,
        "task_type": gap.task_type,
        "severity": gap.severity,
        "finding": gap.message,
        "recommendation": gap.recommendation,
        "decision": None,
        "approved_by": None,
        "resolved_at": None,
    }


def analyze_package(
    package_path: Path,
    *,
    esi_tables_path: Path | None = None,
) -> tuple[SSISPackage, dict[str, Any], list[GapItem]]:
    """Parse and analyze one package without invoking an AI model."""
    package = LocalReader().read(package_path)
    complexity = score_package(package)
    gaps = analyze_gaps(package)
    gaps.extend(detect_cdm_patterns(package))

    if esi_tables_path:
        gaps.extend(analyze_esi_reuse(package, load_esi_config(esi_tables_path)))

    dependency_order = build_package_dependency_order(package)
    task_by_id = {task.id: task for task in package.tasks}
    ordered_names = [
        task_by_id[task_id].name
        for task_id in dependency_order
        if task_id in task_by_id
    ]

    report = {
        "package_name": package.name,
        "source_file": package.source_file,
        "source_sha256": _file_sha256(package_path),
        "analyzed_at": _now(),
        "analysis_mode": "deterministic_no_ai",
        "complexity": complexity.model_dump(mode="json"),
        "gap_count": len(gaps),
        "gaps_by_severity": {
            severity: [
                gap.model_dump(mode="json")
                for gap in gaps
                if gap.severity == severity
            ]
            for severity in ("manual_required", "warning", "info")
        },
        "execution_order": ordered_names,
        "connection_managers": [
            {
                "name": manager.name,
                "type": manager.type.value,
                "server": manager.server,
                "database": manager.database,
            }
            for manager in package.connection_managers
        ],
        "parameters": [parameter.name for parameter in package.parameters],
        "variables": [
            variable.name
            for variable in package.variables
            if variable.namespace.lower() == "user"
        ],
        "event_handlers": [handler.event_name for handler in package.event_handlers],
    }
    return package, report, gaps


def convert_package(
    package: SSISPackage,
    output_dir: Path,
    *,
    generate_trigger: bool = True,
    on_prem_ir_name: str = "SelfHostedIR",
    auth_type: str = "SystemAssignedManagedIdentity",
    use_key_vault: bool = False,
    kv_ls_name: str = "LS_KeyVault",
    kv_url: str = "https://TODO.vault.azure.net/",
    pipeline_prefix: str = "PL_",
) -> dict[str, Any]:
    """Generate the deterministic conversion baseline for one parsed package."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cdm_gaps = detect_cdm_patterns(package)

    linked_services = generate_linked_services(
        package,
        output_dir,
        on_prem_ir_name=on_prem_ir_name,
        auth_type=auth_type,
        use_key_vault=use_key_vault,
        kv_ls_name=kv_ls_name,
        kv_url=kv_url,
    )
    datasets = generate_datasets(package, output_dir)
    data_flows = generate_data_flows(package, output_dir)
    pipeline = generate_pipeline(
        package,
        output_dir,
        stubs_dir=output_dir / "stubs",
        llm_translate=False,
        pipeline_prefix=pipeline_prefix,
        cdm_gaps=cdm_gaps,
    )
    triggers = generate_triggers(package, output_dir) if generate_trigger else []
    stub_files = sorted((output_dir / "stubs").rglob("*.py"))

    return {
        "conversion_mode": "deterministic_no_ai",
        "output_directory": str(output_dir),
        "artifacts_generated": {
            "pipelines": 1,
            "linked_services": len(linked_services),
            "datasets": len(datasets),
            "data_flows": len(data_flows),
            "triggers": len(triggers),
            "azure_function_stubs": len(stub_files),
        },
        "files": {
            "pipeline": str(output_dir / "pipeline" / f"{pipeline['name']}.json"),
            "linked_services": [service["name"] for service in linked_services],
            "datasets": [dataset["name"] for dataset in datasets],
            "data_flows": [data_flow["name"] for data_flow in data_flows],
            "stubs": [str(path) for path in stub_files],
        },
    }


def validate_artifacts(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Run structural validation without constructing Azure credentials."""
    deployer = AdfDeployer.__new__(AdfDeployer)
    return deployer.validate_artifacts(artifacts_dir)


def _report_markdown(manifest: dict[str, Any], review_queue: list[dict[str, Any]]) -> str:
    packages = manifest["packages"]
    converted = sum(1 for package in packages if package["conversion_status"] == "converted")
    uncertain = sum(1 for item in review_queue if item["status"] == "open")
    lines = [
        f"# Migration kickoff report: {manifest['migration_name']}",
        "",
        "The deterministic engine analyzed and converted known SSIS patterns without AI.",
        "Copilot should spend reasoning time only on the open review queue "
        "and final semantic review.",
        "",
        "## Summary",
        "",
        f"- Packages discovered: **{len(packages)}**",
        f"- Converted with no package-specific gaps: **{converted}**",
        f"- Open AI/review/architecture findings: **{uncertain}**",
        f"- Package failures: **{len(manifest['errors'])}**",
        "",
        "## Packages",
        "",
        "| Package | Complexity | Conversion status | Open review items |",
        "|---|---:|---|---:|",
    ]
    for package in packages:
        lines.append(
            f"| {package['name']} | {package['complexity']['score']} "
            f"({package['complexity']['effort_estimate']}) | "
            f"{package['conversion_status']} | {package['open_review_items']} |"
        )

    lines.extend([
        "",
        "## Next action for Copilot",
        "",
        "1. Read `02_assessment/ai_review_queue.json`.",
        "2. Investigate only items whose status is `open`.",
        "3. Compare proposed changes with the original `.dtsx` semantics.",
        "4. Record each approved decision with `ssis-adf review`.",
        "5. Record G1 assessment approval with `ssis-adf gate`.",
        "6. Run `ssis-adf validate` after changes.",
        "7. Perform final semantic review, testing, and reconciliation before approval.",
        "",
        "> Generated artifacts are a conversion baseline, not proof of equivalence.",
    ])
    return "\n".join(lines) + "\n"


def run_migration(
    source: Path,
    workspace: Path,
    *,
    recursive: bool = True,
    generate_trigger: bool = True,
    on_prem_ir_name: str = "SelfHostedIR",
    auth_type: str = "SystemAssignedManagedIdentity",
    use_key_vault: bool = False,
    kv_url: str = "https://TODO.vault.azure.net/",
) -> dict[str, Any]:
    """Kick off inventory, analysis, deterministic conversion, and AI triage."""
    source = source.resolve()
    workspace = workspace.resolve()
    package_paths = LocalReader().scan(source, recursive=recursive)
    if not package_paths:
        raise ValueError(f"No .dtsx packages found under {source}")

    inventory_dir = workspace / "01_inventory"
    assessment_dir = workspace / "02_assessment"
    artifacts_root = workspace / "03_adf"
    review_dir = workspace / "04_review"
    reconciliation_dir = workspace / "05_reconciliation"
    for path in (
        inventory_dir,
        assessment_dir,
        artifacts_root,
        review_dir,
        reconciliation_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    migration_name = workspace.name
    manifest: dict[str, Any] = {
        "workflow_version": WORKFLOW_VERSION,
        "migration_name": migration_name,
        "created_at": _now(),
        "updated_at": _now(),
        "source": {"type": "local", "path": str(source), "recursive": recursive},
        "policy": {
            "deterministic_first": True,
            "ai_used_for_obvious_mappings": False,
            "ai_scope": "open review queue, semantic review, mismatch diagnosis",
            "reconciliation_is_proof": True,
        },
        "packages": [],
        "errors": [],
    }
    inventory: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []
    used_keys: set[str] = set()

    for package_path in package_paths:
        package_key = _safe_name(package_path.stem)
        if package_key in used_keys:
            suffix = hashlib.sha256(str(package_path).encode()).hexdigest()[:8]
            package_key = f"{package_key}_{suffix}"
        used_keys.add(package_key)

        try:
            package, analysis, gaps = analyze_package(package_path)
            review_items = [
                _review_item(package_key, index, gap)
                for index, gap in enumerate(gaps, start=1)
                if _review_route(gap) != "converted"
            ]
            review_queue.extend(review_items)

            conversion = convert_package(
                package,
                artifacts_root / package_key,
                generate_trigger=generate_trigger,
                on_prem_ir_name=on_prem_ir_name,
                auth_type=auth_type,
                use_key_vault=use_key_vault,
                kv_url=kv_url,
            )
            _write_json(assessment_dir / f"{package_key}.analysis.json", analysis)
            _write_json(assessment_dir / f"{package_key}.conversion.json", conversion)

            open_routes = {item["route"] for item in review_items}
            if "architecture_decision_required" in open_routes:
                conversion_status = "architecture_decision_required"
            elif "ai_refactor_required" in open_routes:
                conversion_status = "ai_refactor_required"
            elif "review_required" in open_routes:
                conversion_status = "review_required"
            else:
                conversion_status = "converted"

            inventory.append({
                "key": package_key,
                "name": package.name,
                "path": str(package_path),
                "sha256": analysis["source_sha256"],
            })
            manifest["packages"].append({
                "key": package_key,
                "name": package.name,
                "source_path": str(package_path),
                "source_sha256": analysis["source_sha256"],
                "complexity": analysis["complexity"],
                "conversion_status": conversion_status,
                "open_review_items": len(review_items),
                "artifacts_dir": str(artifacts_root / package_key),
                "phase": "ai_review" if review_items else "semantic_review",
                "gates": {
                    "G1_assessment": "pending",
                    "G2_build": "pending",
                    "G3_test": "pending",
                    "G4_reconciliation": "pending",
                    "G5_cutover": "pending",
                },
            })
        except Exception as exc:
            manifest["errors"].append({
                "package_path": str(package_path),
                "error": str(exc),
            })

    _write_json(inventory_dir / "packages.json", inventory)
    _write_json(assessment_dir / "ai_review_queue.json", review_queue)
    _write_json(workspace / "migration.json", manifest)
    (workspace / "migration_report.md").write_text(
        _report_markdown(manifest, review_queue),
        encoding="utf-8",
    )
    (review_dir / "semantic_review.md").write_text(
        "# Final semantic review\n\n"
        "For each package, compare the original SSIS control flow, expressions, SQL, "
        "data-flow semantics, variables, error paths, and outputs with the refactored "
        "ADF assets.\n\n"
        "Do not mark equivalence proven here. Record risks and test cases; "
        "reconciliation is proof.\n",
        encoding="utf-8",
    )
    _write_json(reconciliation_dir / "results.json", {"packages": {}})
    return manifest


def load_workspace(workspace: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workspace = workspace.resolve()
    manifest = _read_json(workspace / "migration.json")
    queue = _read_json(workspace / "02_assessment" / "ai_review_queue.json")
    return manifest, queue


def record_review_decision(
    workspace: Path,
    item_id: str,
    decision: str,
    approved_by: str,
) -> dict[str, Any]:
    """Resolve one explicit AI/review finding and retain its approval record."""
    manifest, queue = load_workspace(workspace)
    item = next((candidate for candidate in queue if candidate["id"] == item_id), None)
    if item is None:
        raise ValueError(f"Review item not found: {item_id}")
    item["status"] = "resolved"
    item["decision"] = decision
    item["approved_by"] = approved_by
    item["resolved_at"] = _now()

    for package in manifest["packages"]:
        package_items = [
            candidate
            for candidate in queue
            if candidate["package"] == package["key"] and candidate["status"] == "open"
        ]
        package["open_review_items"] = len(package_items)
        if not package_items and package["phase"] == "ai_review":
            package["phase"] = "semantic_review"
            package["conversion_status"] = "review_complete"
    manifest["updated_at"] = _now()
    _write_json(workspace / "02_assessment" / "ai_review_queue.json", queue)
    _write_json(workspace / "migration.json", manifest)
    return item


def validate_workspace(workspace: Path, package_key: str | None = None) -> dict[str, Any]:
    """Validate converted artifacts and update G2 without bypassing open reviews."""
    manifest, queue = load_workspace(workspace)
    results: list[dict[str, Any]] = []
    selected = [
        package
        for package in manifest["packages"]
        if package_key is None or package["key"] == package_key
    ]
    if not selected:
        raise ValueError(f"Package not found in workspace: {package_key}")

    for package in selected:
        issues = validate_artifacts(Path(package["artifacts_dir"]))
        open_items = [
            item
            for item in queue
            if item["package"] == package["key"] and item["status"] == "open"
        ]
        assessment_approved = package["gates"]["G1_assessment"] == "passed"
        package["gates"]["G2_build"] = (
            "passed" if assessment_approved and not issues and not open_items else "blocked"
        )
        if package["gates"]["G2_build"] == "passed":
            package["phase"] = "test"
        result = {
            "package": package["key"],
            "status": package["gates"]["G2_build"],
            "validation_issues": issues,
            "open_review_items": [item["id"] for item in open_items],
            "assessment_approved": assessment_approved,
        }
        results.append(result)
        _write_json(
            workspace / "04_review" / f"{package['key']}.validation.json",
            result,
        )

    manifest["updated_at"] = _now()
    _write_json(workspace / "migration.json", manifest)
    return {"packages": results}


def record_reconciliation(
    workspace: Path,
    package_key: str,
    result: str,
    evidence: Path,
    notes: str = "",
) -> dict[str, Any]:
    """Record reconciliation and route mismatches back into the AI review queue."""
    if result not in {"pass", "fail"}:
        raise ValueError("Reconciliation result must be 'pass' or 'fail'")
    if not evidence.exists():
        raise ValueError(f"Evidence file does not exist: {evidence}")

    manifest, queue = load_workspace(workspace)
    package = next(
        (candidate for candidate in manifest["packages"] if candidate["key"] == package_key),
        None,
    )
    if package is None:
        raise ValueError(f"Package not found in workspace: {package_key}")
    if package["gates"]["G3_test"] != "passed":
        raise ValueError(
            f"Package {package_key} cannot reconcile: G3_test is "
            f"{package['gates']['G3_test']}. Record successful test evidence first."
        )

    results_path = workspace / "05_reconciliation" / "results.json"
    results = _read_json(results_path)
    record = {
        "result": result,
        "evidence": str(evidence.resolve()),
        "evidence_sha256": _file_sha256(evidence),
        "notes": notes,
        "recorded_at": _now(),
    }
    results["packages"][package_key] = record

    if result == "pass":
        package["gates"]["G4_reconciliation"] = "passed"
        package["phase"] = "parallel_run"
    else:
        package["gates"]["G4_reconciliation"] = "failed"
        package["phase"] = "ai_review"
        mismatch_number = 1 + sum(
            1 for item in queue if item["id"].startswith(f"{package_key}-RECON-")
        )
        queue.append({
            "id": f"{package_key}-RECON-{mismatch_number:03d}",
            "package": package_key,
            "route": "ai_refactor_required",
            "status": "open",
            "task_id": None,
            "task_name": "Reconciliation mismatch",
            "task_type": "Reconciliation",
            "severity": "manual_required",
            "finding": notes or "ADF output did not reconcile with the SSIS baseline.",
            "recommendation": (
                "Copilot must diagnose the mismatch against the original SSIS semantics, "
                "propose a reviewed fix, then repeat validation, testing, and reconciliation."
            ),
            "decision": None,
            "approved_by": None,
            "resolved_at": None,
        })
        package["open_review_items"] += 1

    manifest["updated_at"] = _now()
    _write_json(results_path, results)
    _write_json(workspace / "02_assessment" / "ai_review_queue.json", queue)
    _write_json(workspace / "migration.json", manifest)
    return record


def record_gate_evidence(
    workspace: Path,
    package_key: str,
    gate: str,
    result: str,
    evidence: Path,
    approved_by: str,
    notes: str = "",
) -> dict[str, Any]:
    """Record human/test evidence for gates that cannot be inferred from artifacts."""
    gate_map = {
        "G1": "G1_assessment",
        "G3": "G3_test",
        "G5": "G5_cutover",
    }
    if gate not in gate_map:
        raise ValueError("Gate must be one of: G1, G3, G5")
    if result not in {"pass", "fail"}:
        raise ValueError("Gate result must be 'pass' or 'fail'")
    if not evidence.exists():
        raise ValueError(f"Evidence file does not exist: {evidence}")

    manifest, _ = load_workspace(workspace)
    package = next(
        (candidate for candidate in manifest["packages"] if candidate["key"] == package_key),
        None,
    )
    if package is None:
        raise ValueError(f"Package not found in workspace: {package_key}")
    if gate == "G3" and package["gates"]["G2_build"] != "passed":
        raise ValueError("G3 test evidence cannot be recorded before G2_build passes")
    if gate == "G5" and package["gates"]["G4_reconciliation"] != "passed":
        raise ValueError("G5 cutover approval cannot be recorded before G4_reconciliation passes")

    gate_key = gate_map[gate]
    package["gates"][gate_key] = "passed" if result == "pass" else "failed"
    if result == "pass":
        if gate == "G1":
            package["phase"] = (
                "ai_review" if package["open_review_items"] else "semantic_review"
            )
        elif gate == "G3":
            package["phase"] = "reconciliation"
        elif gate == "G5":
            package["phase"] = "complete"

    record = {
        "package": package_key,
        "gate": gate_key,
        "result": result,
        "evidence": str(evidence.resolve()),
        "evidence_sha256": _file_sha256(evidence),
        "approved_by": approved_by,
        "notes": notes,
        "recorded_at": _now(),
    }
    manifest["updated_at"] = _now()
    _write_json(workspace / "evidence" / f"{package_key}.{gate}.json", record)
    _write_json(workspace / "migration.json", manifest)
    return record


def deploy_workspace(
    workspace: Path,
    package_key: str,
    *,
    subscription_id: str,
    resource_group: str,
    factory_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Deploy one package only after the machine-enforced G2 build gate passes."""
    manifest, _ = load_workspace(workspace)
    package = next(
        (candidate for candidate in manifest["packages"] if candidate["key"] == package_key),
        None,
    )
    if package is None:
        raise ValueError(f"Package not found in workspace: {package_key}")
    if package["gates"]["G2_build"] != "passed":
        raise ValueError(
            f"Package {package_key} cannot deploy: G2_build is "
            f"{package['gates']['G2_build']}. Resolve reviews and validate first."
        )

    deployer = AdfDeployer(
        subscription_id=subscription_id,
        resource_group=resource_group,
        factory_name=factory_name,
    )
    results = deployer.deploy_all(Path(package["artifacts_dir"]), dry_run=dry_run)
    summary = {
        "package": package_key,
        "dry_run": dry_run,
        "total": len(results),
        "succeeded": sum(1 for result in results if result.success),
        "failed": sum(1 for result in results if not result.success),
        "results": [
            {
                "type": result.artifact_type,
                "name": result.name,
                "success": result.success,
                "error": result.error,
            }
            for result in results
        ],
    }
    _write_json(workspace / "06_release" / f"{package_key}.deployment.json", summary)
    return summary
