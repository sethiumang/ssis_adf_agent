"""Check that ssis-adf-agent is installed correctly.

Starts the MCP server the same way VS Code does (stdio), lists its tools, and analyzes a
sample package. Run from the repo root inside your virtual environment:

    python scripts/verify_install.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "samples" / "fsi" / "ssis" / "ContosoBank.EOD" / "FIN_FX_DailyRates_Load.dtsx"
EXPECTED_TOOLS = {"scan_ssis_packages", "analyze_ssis_package", "convert_ssis_package",
                  "validate_adf_artifacts", "deploy_to_adf"}


async def _check() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "ssis_adf_agent.mcp_server"],
                                   cwd=str(REPO))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        missing = EXPECTED_TOOLS - tools
        if missing:
            raise SystemExit(f"FAIL: MCP server is missing tools: {sorted(missing)}")
        print(f"OK   MCP server started; tools: {', '.join(sorted(tools))}")

        result = await session.call_tool("analyze_ssis_package", {"package_path": str(SAMPLE)})
        analysis = json.loads(result.content[0].text)
        c = analysis["complexity"]
        print(f"OK   analyzed {analysis['package_name']}: {c['total_tasks']} tasks, "
              f"complexity {c['score']} ({c['effort_estimate']})")


def main() -> int:
    if sys.version_info < (3, 11):  # noqa: UP036 - users may run this with the wrong interpreter
        print(f"FAIL: Python 3.11+ required, found {sys.version.split()[0]}")
        return 1
    try:
        import ssis_adf_agent  # noqa: F401
    except ImportError:
        print("FAIL: ssis_adf_agent is not installed. Activate your venv and run: pip install -e .")
        return 1
    asyncio.run(_check())
    print("\nAll checks passed. Open this folder in VS Code, start Copilot Chat in Agent mode,\n"
          "and follow docs/CUSTOMER_FLOW.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
