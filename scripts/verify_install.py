"""Check that the CLI-first SSIS-to-ADF workflow is installed correctly.

Analyzes and converts a sample package without invoking MCP or an AI model. Run from the
repo root inside your virtual environment:

    python scripts/verify_install.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "samples" / "fsi" / "ssis" / "ContosoBank.EOD" / "FIN_FX_DailyRates_Load.dtsx"


def main() -> int:
    if sys.version_info < (3, 11):  # noqa: UP036 - users may run this with the wrong interpreter
        print(f"FAIL: Python 3.11+ required, found {sys.version.split()[0]}")
        return 1
    try:
        import ssis_adf_agent  # noqa: F401
    except ImportError:
        print("FAIL: ssis_adf_agent is not installed. Activate your venv and run: pip install -e .")
        return 1

    from ssis_adf_agent.workflow import analyze_package, convert_package, validate_artifacts

    package, analysis, gaps = analyze_package(SAMPLE)
    complexity = analysis["complexity"]
    print(
        f"OK   analyzed {analysis['package_name']}: {complexity['total_tasks']} tasks, "
        f"complexity {complexity['score']} ({complexity['effort_estimate']}), "
        f"{len(gaps)} review findings"
    )

    with tempfile.TemporaryDirectory(prefix="ssis-adf-verify-") as temp_dir:
        output_dir = Path(temp_dir) / "adf"
        conversion = convert_package(package, output_dir)
        issues = validate_artifacts(output_dir)
        if issues:
            print(f"FAIL: generated sample artifacts have {len(issues)} validation issue(s)")
            return 1
        count = conversion["artifacts_generated"]
        print(
            "OK   deterministic conversion generated "
            f"{count['pipelines']} pipeline, {count['linked_services']} linked services, "
            f"{count['datasets']} datasets, and {count['data_flows']} data flows"
        )

    print(
        "\nAll checks passed. Run `ssis-adf migrate --help`, then follow "
        "docs/CUSTOMER_FLOW.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
