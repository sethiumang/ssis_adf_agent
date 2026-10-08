"""Generate the Contoso Bank FSI SSIS sample corpus.

Usage (from repo root):
    python samples/fsi/tools/generate_packages.py          # write files
    python samples/fsi/tools/generate_packages.py --check  # fail if committed files are stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import PROJECT_NAME, PROJECT_PARAMS  # noqa: E402
from dtsx_builder import DTS_NS, project_params_xml  # noqa: E402
from packages_a import aml_ctr_monitoring, custodian_positions, customer_kyc_scd2, fx_daily_rates, gl_incremental  # noqa: E402
from packages_b import ach_nacha_outbound, bank_statement_recon, credit_exposure, eod_master, ofac_screening  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = ROOT / "ssis" / PROJECT_NAME
CATALOG = ROOT / "catalog.json"

BUILDERS = [fx_daily_rates, gl_incremental, custodian_positions, customer_kyc_scd2, aml_ctr_monitoring,
            ach_nacha_outbound, bank_statement_recon, credit_exposure, ofac_screening, eod_master]

D = f"{{{DTS_NS}}}"


def _inventory(xml_text: str) -> dict:
    """Ground-truth counts computed from the generated XML (used as parser test oracle)."""
    root = ET.fromstring(xml_text)
    tasks: Counter[str] = Counter()
    containers: Counter[str] = Counter()
    for exe in root.iter(f"{D}Executable"):
        if exe is root:
            continue
        kind = exe.get(f"{D}CreationName", "")
        (containers if kind.startswith("STOCK:") else tasks)[kind] += 1
    components = Counter(c.get("componentClassID") for c in root.iter("component"))
    constraints = list(root.iter(f"{D}PrecedenceConstraint"))
    return {
        "connection_managers": sorted({cm.get(f"{D}CreationName") + ":" + cm.get(f"{D}ObjectName")
                                       for cm in root.iter(f"{D}ConnectionManager")
                                       if cm.get(f"{D}ObjectName")}),
        "package_parameters": [p.get(f"{D}ObjectName") for p in root.iter(f"{D}PackageParameter")],
        "user_variables": sorted({v.get(f"{D}ObjectName") for v in root.iter(f"{D}Variable")
                                  if v.get(f"{D}Namespace") == "User"}),
        "tasks": dict(sorted(tasks.items())),
        "containers": dict(sorted(containers.items())),
        "task_count": sum(tasks.values()),
        "container_count": sum(containers.values()),
        "data_flow_components": dict(sorted(components.items())),
        "data_flow_component_count": sum(components.values()),
        "data_flow_paths": sum(1 for _ in root.iter("path")),
        "precedence_constraints": len(constraints),
        "expression_constraints": sum(1 for c in constraints if c.get(f"{D}Expression")),
        "or_constraints": sum(1 for c in constraints if c.get(f"{D}LogicalAnd") == "False"),
        "failure_constraints": sum(1 for c in constraints if c.get(f"{D}Value") == "1"),
        "completion_constraints": sum(1 for c in constraints if c.get(f"{D}Value") == "2"),
        "event_handlers": [h.get(f"{D}EventName") for h in root.iter(f"{D}EventHandler")],
        "protection_level": int(root.get(f"{D}ProtectionLevel", "0")),
    }


def build() -> dict[Path, str]:
    files: dict[Path, str] = {}
    catalog = {
        "project": PROJECT_NAME,
        "description": "Synthetic Contoso Bank end-of-day SSIS project for testing SSIS -> ADF migration tooling. "
                       "All names, hosts and data are fictional.",
        "ssis_version": "SQL Server 2019 (PackageFormatVersion 8, project deployment model)",
        "generator": "samples/fsi/tools/generate_packages.py",
        "packages": [],
    }
    for idx, builder in enumerate(BUILDERS, start=1):
        pkg, meta = builder()
        xml_text = pkg.to_xml()
        rel = f"ssis/{PROJECT_NAME}/{pkg.name}.dtsx"
        files[ROOT / rel] = xml_text
        catalog["packages"].append({
            "id": f"{idx:02d}",
            "name": pkg.name,
            "file": rel,
            "description": pkg.description,
            **meta,
            "sha256": hashlib.sha256(xml_text.encode("utf-8")).hexdigest(),
            "inventory": _inventory(xml_text),
        })
    files[PROJECT_DIR / "Project.params"] = project_params_xml(PROJECT_PARAMS)
    catalog["project_parameters"] = [
        {"name": n, "type": t, "required": r, "sensitive": s, "description": desc}
        for n, t, _, r, s, desc in PROJECT_PARAMS]
    files[CATALOG] = json.dumps(catalog, indent=2) + "\n"
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify committed files match generator output")
    args = parser.parse_args()
    files = build()
    stale = []
    for path, text in files.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT.parents[1])}")
    if stale:
        print("Stale generated files:\n  " + "\n  ".join(str(p) for p in stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
