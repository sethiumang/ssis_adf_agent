"""Validate the Contoso Bank FSI SSIS corpus and probe the ssis_adf_agent pipeline against it.

Three layers of checks:
  1. Integrity   - every package is well-formed XML and all internal references resolve
                   (precedence constraints, data-flow paths, lineage IDs, connection managers,
                   Execute SQL connections, variables used in expressions / mappings).
  2. Parser parity - the repo's SSISParser output is compared with ground-truth counts in catalog.json.
                   Mismatches are *findings about the parser*, not corpus failures.
  3. Pipeline probe - complexity score, gap analysis, conversion to ADF JSON and structural validation.

Usage (from repo root, with the repo installed in the active environment):
    python samples/fsi/tools/validate_corpus.py [--json report.json] [--keep-output DIR]

Exit code is non-zero only if layer 1 (corpus integrity) fails or a package crashes the pipeline.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"
DTS = "www.microsoft.com/SqlServer/Dts"
SQLTASK = "www.microsoft.com/sqlserver/dts/tasks/sqltask"
D = f"{{{DTS}}}"
VAR_REF = re.compile(r"@\[(User|System)::([^\]]+)\]")
LINEAGE_REF = re.compile(r"#\{([^}]+)\}")


# --------------------------------------------------------------------------- layer 1
def check_integrity(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        root = etree.parse(str(path)).getroot()
    except etree.XMLSyntaxError as exc:
        return [f"not well-formed: {exc}"]

    ref_ids = [e.get(f"{D}refId") or e.get("refId") for e in root.iter()
               if isinstance(e.tag, str) and (e.get(f"{D}refId") or e.get("refId"))]
    dupes = [r for r, n in Counter(ref_ids).items() if n > 1]
    if dupes:
        errors.append(f"duplicate refIds: {dupes[:5]}")

    exe_refs = {e.get(f"{D}refId") for e in root.iter(f"{D}Executable")}
    for pc in root.iter(f"{D}PrecedenceConstraint"):
        for side in ("From", "To"):
            if pc.get(f"{D}{side}") not in exe_refs:
                errors.append(f"constraint {pc.get(f'{D}refId')} {side} unresolved: {pc.get(f'{D}{side}')}")

    cm_refs = {cm.get(f"{D}refId") for cm in root.iter(f"{D}ConnectionManager") if cm.get(f"{D}refId")}
    cm_ids = {cm.get(f"{D}DTSID") for cm in root.iter(f"{D}ConnectionManager") if cm.get(f"{D}DTSID")}
    for sql in root.iter(f"{{{SQLTASK}}}SqlTaskData"):
        if sql.get(f"{{{SQLTASK}}}Connection") not in cm_ids:
            errors.append(f"Execute SQL connection unresolved: {sql.get(f'{{{SQLTASK}}}Connection')}")
    for conn in root.iter("connection"):
        if conn.get("connectionManagerRefId") not in cm_refs:
            errors.append(f"component connection unresolved: {conn.get('connectionManagerRefId')}")
    for exe in root.iter(f"{D}Executable"):
        ot = exe.find(f"{D}ObjectData")
        if ot is None:
            continue
        for child in ot.iter():
            for attr in ("Connection", "SMTPServer", "Source", "Destination"):
                for key, val in child.attrib.items():
                    if key.endswith(attr) and val and val.startswith("{") and val not in cm_ids:
                        errors.append(f"{exe.get(f'{D}ObjectName')}: {key} unresolved {val}")

    out_ports = {o.get("refId") for o in root.iter("output")}
    in_ports = {i.get("refId") for i in root.iter("input")}
    connected_inputs: Counter[str] = Counter()
    for p in root.iter("path"):
        if p.get("startId") not in out_ports:
            errors.append(f"path start unresolved: {p.get('startId')}")
        if p.get("endId") not in in_ports:
            errors.append(f"path end unresolved: {p.get('endId')}")
        connected_inputs[p.get("endId")] += 1
    for port, n in connected_inputs.items():
        if n > 1:
            errors.append(f"input fed by {n} paths: {port}")
    for comp in root.iter("component"):
        for inp in comp.iter("input"):
            if inp.get("refId") not in connected_inputs:
                errors.append(f"input not connected: {inp.get('refId')}")

    lineage = {c.get("lineageId") for c in root.iter("outputColumn") if c.get("lineageId")}
    input_cols = {c.get("refId") for c in root.iter("inputColumn")}
    for ic in root.iter("inputColumn"):
        if ic.get("lineageId") not in lineage:
            errors.append(f"inputColumn lineage unresolved: {ic.get('refId')}")
    for prop in root.iter("property"):
        valid = input_cols if prop.get("name") == "InputColumnID" else lineage
        for ref in LINEAGE_REF.findall(prop.text or ""):
            if ref not in valid:
                errors.append(f"expression lineage unresolved in {prop.get('name')}: {ref}")

    user_vars = {v.get(f"{D}ObjectName") for v in root.iter(f"{D}Variable") if v.get(f"{D}Namespace") == "User"}
    texts = [v for e in root.iter() if isinstance(e.tag, str) for v in [*e.attrib.values(), e.text or ""]]
    for t in texts:
        for ns, name in VAR_REF.findall(t):
            if ns == "User" and name not in user_vars:
                errors.append(f"undeclared variable @[User::{name}]")
    for vname in {m for t in texts for m in re.findall(r"^User::(\w+)$", t)}:
        if vname not in user_vars:
            errors.append(f"undeclared variable mapping User::{vname}")
    return sorted(set(errors))


# --------------------------------------------------------------------------- layer 2
def _walk(tasks):
    for t in tasks:
        yield t
        yield from _walk(getattr(t, "tasks", []) or [])


def _walk_constraints(pkg):
    yield from pkg.constraints
    for t in _walk(pkg.tasks):
        yield from getattr(t, "constraints", []) or []


def parser_parity(pkg, truth: dict) -> list[str]:
    from ssis_adf_agent.parsers.models import TaskType

    findings: list[str] = []
    tasks = list(_walk(pkg.tasks)) + [t for eh in pkg.event_handlers for t in _walk(eh.tasks)]
    container_types = {TaskType.SEQUENCE, TaskType.FOREACH_LOOP, TaskType.FOR_LOOP}
    n_tasks = sum(1 for t in tasks if t.task_type not in container_types)
    n_cont = sum(1 for t in tasks if t.task_type in container_types)

    def cmp(label, got, want):
        if got != want:
            findings.append(f"{label}: parser={got} expected={want}")

    cmp("tasks", n_tasks, truth["task_count"])
    cmp("containers", n_cont, truth["container_count"])
    cmp("connection_managers", len(pkg.connection_managers), len(truth["connection_managers"]))
    cmp("package_parameters", len(pkg.parameters), len(truth["package_parameters"]))
    cmp("user_variables", len({v.name for v in pkg.variables if v.namespace == "User"}),
        len(truth["user_variables"]))
    cmp("event_handlers", len(pkg.event_handlers), len(truth["event_handlers"]))
    cons = list(_walk_constraints(pkg))
    for eh in pkg.event_handlers:
        cons += eh.constraints
    cmp("precedence_constraints", len(cons), truth["precedence_constraints"])
    cmp("or_constraints", sum(1 for c in cons if not c.logical_and), truth["or_constraints"])
    cmp("expression_constraints", sum(1 for c in cons if c.expression), truth["expression_constraints"])
    dft = [t for t in tasks if t.task_type == TaskType.DATA_FLOW]
    cmp("data_flow_components", sum(len(t.components) for t in dft), truth["data_flow_component_count"])
    cmp("data_flow_paths", sum(len(t.paths) for t in dft), truth["data_flow_paths"])

    task_ids = {t.id for t in tasks}
    dangling = [c for c in cons if c.from_task_id not in task_ids or c.to_task_id not in task_ids]
    if dangling:
        findings.append(f"constraints whose from/to do not match any parsed task id: {len(dangling)}/{len(cons)}")
    unknown = [t.name for t in tasks if t.task_type == TaskType.UNKNOWN]
    if unknown:
        findings.append(f"UNKNOWN task types: {unknown}")
    generic_comps = sorted({c.component_class_id for t in dft for c in t.components
                            if c.component_type in ("", "Unknown", c.component_class_id)})
    if generic_comps:
        findings.append(f"components without a recognised component_type: {generic_comps}")
    if truth["user_variables"] and all(v.data_type == "String" for v in pkg.variables if v.namespace == "User"):
        findings.append("all user variables parsed as String (data types lost)")
    if truth["protection_level"] and pkg.protection_level.value in ("DontSaveSensitive", "0"):
        findings.append(f"protection level not detected (expected {truth['protection_level']})")
    return findings


# --------------------------------------------------------------------------- layer 3
def probe_pipeline(path: Path, out_dir: Path) -> dict:
    # Mirrors mcp_server._convert / _validate without importing the MCP server module.
    from ssis_adf_agent.analyzers.cdm_pattern_detector import detect_cdm_patterns
    from ssis_adf_agent.analyzers.complexity_scorer import score_package
    from ssis_adf_agent.analyzers.gap_analyzer import analyze_gaps
    from ssis_adf_agent.deployer.adf_deployer import AdfDeployer
    from ssis_adf_agent.generators.dataflow_generator import generate_data_flows
    from ssis_adf_agent.generators.dataset_generator import generate_datasets
    from ssis_adf_agent.generators.linked_service_generator import generate_linked_services
    from ssis_adf_agent.generators.pipeline_generator import generate_pipeline
    from ssis_adf_agent.generators.trigger_generator import generate_triggers
    from ssis_adf_agent.parsers.readers.local_reader import LocalReader

    pkg = LocalReader().read(path)
    score = score_package(pkg)
    gaps = analyze_gaps(pkg)
    ls = generate_linked_services(pkg, out_dir)
    ds = generate_datasets(pkg, out_dir)
    dfs = generate_data_flows(pkg, out_dir)
    pl = generate_pipeline(pkg, out_dir, stubs_dir=out_dir / "stubs", cdm_gaps=detect_cdm_patterns(pkg))
    trg = generate_triggers(pkg, out_dir)
    acts = pl.get("properties", {}).get("activities", [])
    manual = [a for a in acts if any(k in a.get("description", "") for k in ("MANUAL REVIEW", "UNSUPPORTED"))]
    issues = AdfDeployer.__new__(AdfDeployer).validate_artifacts(out_dir)
    dangling = _dangling_references(out_dir)
    return {
        "package": pkg,
        "complexity": {"score": score.score, "effort": score.effort_estimate},
        "gaps": Counter(g.severity for g in gaps),
        "gap_items": [f"[{g.severity}] {g.task_name}: {g.message}" for g in gaps],
        "artifacts": {"activities": len(acts), "linked_services": len(ls), "datasets": len(ds),
                      "data_flows": len(dfs), "triggers": len(trg),
                      "stubs": len(list((out_dir / "stubs").rglob("*.py")))},
        "manual_review": len(manual),
        "adf_validation": "valid" if not issues else f"{len(issues)} issues",
        "adf_issues": issues,
        "dangling_references": dangling,
    }


_REF_FOLDERS = {"DataFlowReference": "dataflow", "LinkedServiceReference": "linkedService",
                "DatasetReference": "dataset", "PipelineReference": "pipeline"}


def _dangling_references(out_dir: Path) -> list[str]:
    """ADF references that point at artifacts not present in the output (deploy would fail)."""
    present = {folder: {p.stem for p in (out_dir / folder).glob("*.json")} for folder in _REF_FOLDERS.values()}
    missing: set[str] = set()

    def visit(node):
        if isinstance(node, dict):
            folder = _REF_FOLDERS.get(node.get("type", ""))
            name = node.get("referenceName")
            if folder and isinstance(name, str) and folder != "pipeline" and name not in present[folder]:
                missing.add(f"{node['type']}:{name}")
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    for f in out_dir.rglob("*.json"):
        if "stubs" not in f.parts:
            visit(json.loads(f.read_text(encoding="utf-8")))
    return sorted(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, help="write the full report as JSON")
    ap.add_argument("--keep-output", type=Path, help="directory to keep generated ADF artifacts")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    report, hard_fail = [], False
    tmp = tempfile.TemporaryDirectory()
    base_out = args.keep_output or Path(tmp.name)

    for entry in catalog["packages"]:
        path = ROOT / entry["file"]
        row: dict = {"id": entry["id"], "name": entry["name"]}
        row["integrity_errors"] = check_integrity(path)
        hard_fail |= bool(row["integrity_errors"])
        try:
            probe = probe_pipeline(path, base_out / entry["name"])
            row["parser_findings"] = parser_parity(probe.pop("package"), entry["inventory"])
            row.update(probe)
            row["gaps"] = dict(row["gaps"])
        except Exception as exc:  # noqa: BLE001
            hard_fail = True
            row["crash"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc()
        report.append(row)

        status = "FAIL" if row["integrity_errors"] or "crash" in row else "ok"
        print(f"\n[{entry['id']}] {entry['name']}  integrity={status}")
        for e in row["integrity_errors"][:10]:
            print(f"    ! {e}")
        if "crash" in row:
            print(f"    CRASH {row['crash']}")
            continue
        print(f"    complexity={row['complexity']}  gaps={row['gaps']}  artifacts={row['artifacts']}"
              f"  manual_review={row['manual_review']}  adf_validation={row['adf_validation']}")
        for f in row["dangling_references"]:
            print(f"    dangling ADF reference: {f}")
        for f in row["parser_findings"]:
            print(f"    parser: {f}")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.cleanup()
    print("\nCorpus integrity:", "FAILED" if hard_fail else "PASSED")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
