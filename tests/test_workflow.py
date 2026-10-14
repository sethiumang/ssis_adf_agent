from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ssis_adf_agent.workflow import (
    deploy_workspace,
    load_workspace,
    record_gate_evidence,
    record_reconciliation,
    run_migration,
    validate_workspace,
)

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "samples" / "fsi" / "ssis" / "ContosoBank.EOD"


def _workspace_for(tmp_path: Path, package_name: str) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(SAMPLES / f"{package_name}.dtsx", source)
    workspace = tmp_path / "workspace"
    run_migration(source, workspace)
    return workspace


def test_migration_converts_known_package_without_ai_queue(tmp_path: Path) -> None:
    workspace = _workspace_for(tmp_path, "FIN_FX_DailyRates_Load")
    manifest, queue = load_workspace(workspace)

    assert manifest["policy"]["deterministic_first"] is True
    assert manifest["policy"]["ai_used_for_obvious_mappings"] is False
    assert manifest["packages"][0]["conversion_status"] == "converted"
    assert queue == []

    evidence = workspace / "migration_report.md"
    record_gate_evidence(
        workspace,
        "FIN_FX_DailyRates_Load",
        "G1",
        "pass",
        evidence,
        "Migration Owner",
    )
    validation = validate_workspace(workspace, "FIN_FX_DailyRates_Load")
    assert validation["packages"][0]["status"] == "passed"
    assert manifest["errors"] == []


def test_uncertain_package_creates_focused_review_queue(tmp_path: Path) -> None:
    workspace = _workspace_for(tmp_path, "PAY_ACH_NACHA_Outbound")
    manifest, queue = load_workspace(workspace)

    assert manifest["packages"][0]["conversion_status"] == "architecture_decision_required"
    assert queue
    assert {item["route"] for item in queue} <= {
        "review_required",
        "ai_refactor_required",
        "architecture_decision_required",
    }
    assert all(item["status"] == "open" for item in queue)

    validation = validate_workspace(workspace, "PAY_ACH_NACHA_Outbound")
    assert validation["packages"][0]["status"] == "blocked"
    assert validation["packages"][0]["open_review_items"]


def test_failed_reconciliation_routes_back_to_ai_review(tmp_path: Path) -> None:
    workspace = _workspace_for(tmp_path, "FIN_FX_DailyRates_Load")
    assessment = workspace / "migration_report.md"
    record_gate_evidence(
        workspace,
        "FIN_FX_DailyRates_Load",
        "G1",
        "pass",
        assessment,
        "Migration Owner",
    )
    validate_workspace(workspace, "FIN_FX_DailyRates_Load")
    evidence = tmp_path / "reconciliation.json"
    evidence.write_text(json.dumps({"mismatched_keys": ["USD-2026-01-01"]}), encoding="utf-8")
    record_gate_evidence(
        workspace,
        "FIN_FX_DailyRates_Load",
        "G3",
        "pass",
        evidence,
        "Test Owner",
    )

    record_reconciliation(
        workspace,
        "FIN_FX_DailyRates_Load",
        "fail",
        evidence,
        "One FX-rate business key differs.",
    )
    manifest, queue = load_workspace(workspace)

    assert manifest["packages"][0]["phase"] == "ai_review"
    assert manifest["packages"][0]["gates"]["G4_reconciliation"] == "failed"
    assert queue[-1]["id"].startswith("FIN_FX_DailyRates_Load-RECON-")
    assert queue[-1]["route"] == "ai_refactor_required"
    assert queue[-1]["status"] == "open"


def test_deployment_is_blocked_until_build_gate_passes(tmp_path: Path) -> None:
    workspace = _workspace_for(tmp_path, "FIN_FX_DailyRates_Load")

    with pytest.raises(ValueError, match="G2_build is pending"):
        deploy_workspace(
            workspace,
            "FIN_FX_DailyRates_Load",
            subscription_id="subscription",
            resource_group="resource-group",
            factory_name="factory",
            dry_run=True,
        )
