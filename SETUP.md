# Setup Guide

Step-by-step instructions for getting `ssis-adf-agent` running on a new machine.

---

## 1. Prerequisites

| Requirement | Version | How to check | Install link |
|---|---|---|---|
| **Python** | 3.11+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| **pip** | Latest | `pip --version` | Included with Python |
| **Git** | Any | `git --version` | [git-scm.com](https://git-scm.com/) |
| **VS Code** | Latest stable | `code --version` | [code.visualstudio.com](https://code.visualstudio.com/) |
| **GitHub Copilot extension** | With MCP/Agent support | Extensions sidebar → search "GitHub Copilot" | VS Code Marketplace |

### Optional (depending on features used)

| Requirement | When needed | Install link |
|---|---|---|
| **ODBC Driver for SQL Server 17+** | Scanning packages from SQL Server (`msdb`) or reading SQL Agent schedules | [Microsoft ODBC Driver](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) |
| **Azure CLI** | Deploying to ADF from a developer machine (`az login`) | [Install Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| **Azure OpenAI access** | LLM-powered C# → Python Script Task translation | Requires an Azure OpenAI resource with a GPT-4o deployment |

---

## 2. Clone and Install

```bash
git clone <this-repo-url> ssis_adf_agent
cd ssis_adf_agent
```

### Create a virtual environment (recommended)

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

### Install the package

```bash
# Core install
pip install -e .

# With development tools (pytest, ruff, mypy)
pip install -e ".[dev]"

# With LLM Script Task translation (Azure OpenAI)
pip install -e ".[llm]"

# Everything
pip install -e ".[dev,llm]"
```

### Verify

```bash
python scripts/verify_install.py
```

This starts the MCP server the same way VS Code does, checks that all five tools are registered, and analyzes a sample package. `ssis-adf-agent` itself is the stdio server: if you run it directly, it waits silently for a client.

> **VS Code and virtual environments:** `.vscode/mcp.json` launches `python -m ssis_adf_agent.mcp_server`. Either start VS Code from a terminal where the venv is active (`code .`), or select the venv interpreter. If neither works, change `"command"` in `.vscode/mcp.json` to the venv's Python (`.venv\\Scripts\\python.exe` or `.venv/bin/python`).

---

## 3. Register the MCP Server in VS Code

The workspace already includes `.vscode/mcp.json` which registers the server automatically when you open the workspace. No manual configuration is needed.

If you need to register it in your **user** settings instead (e.g. for use across multiple workspaces):

1. Open **Command Palette** (`Ctrl+Shift+P`) → **Preferences: Open User Settings (JSON)**
2. Add:

```jsonc
{
  "github.copilot.chat.experimental.mcpServers": {
    "ssis-adf-agent": {
      "type": "stdio",
      "command": "ssis-adf-agent",
      "args": []
    }
  }
}
```

> **Virtual environment note:** If you installed into a venv, replace `"command": "ssis-adf-agent"` with the full path:
> - Windows: `"C:\\path\\to\\.venv\\Scripts\\ssis-adf-agent.exe"`
> - macOS/Linux: `"/path/to/.venv/bin/ssis-adf-agent"`

3. Reload VS Code (`Ctrl+Shift+P` → **Developer: Reload Window**).
4. Open **Copilot Chat** → switch to **Agent** mode → confirm five tools appear:
   `scan_ssis_packages`, `analyze_ssis_package`, `convert_ssis_package`, `validate_adf_artifacts`, `deploy_to_adf`

---

## 4. Environment Variables

### Required for deployment

Set these when using `deploy_to_adf` with a service principal (CI/CD):

```powershell
# PowerShell
$env:AZURE_CLIENT_ID     = "<service-principal-app-id>"
$env:AZURE_CLIENT_SECRET  = "<service-principal-secret>"
$env:AZURE_TENANT_ID      = "<azure-ad-tenant-id>"
```

For local development, `az login` is sufficient — no environment variables needed.

### Required for LLM Script Task translation

Set these when using `convert_ssis_package` with `llm_translate=true`:

```powershell
# PowerShell
$env:AZURE_OPENAI_ENDPOINT   = "https://my-resource.openai.azure.com/"
$env:AZURE_OPENAI_API_KEY    = "<your-key>"
$env:AZURE_OPENAI_DEPLOYMENT = "gpt-4o"   # optional, defaults to gpt-4o
```

```bash
# Bash
export AZURE_OPENAI_ENDPOINT="https://my-resource.openai.azure.com/"
export AZURE_OPENAI_API_KEY="<your-key>"
export AZURE_OPENAI_DEPLOYMENT="gpt-4o"
```

---

## 5. Optional Configuration Files

These JSON config files enable enterprise features. Pass their paths as parameters to `analyze_ssis_package` or `convert_ssis_package`.

### ESI Tables Config (`esi_tables_path`)

Maps source systems to tables available in your ESI Azure SQL layer. Used for reuse detection.

```json
{
  "source_system": "SAP",
  "esi_database": "ESI_SAP",
  "tables": ["VBAK", "VBAP", "MARA", "KNA1"]
}
```

### Schema Remap Config (`schema_remap_path`)

Maps on-prem database names to target schemas when consolidating into a single Azure SQL database.

```json
{
  "StagingDB": "staging",
  "ReportingDB": "reporting",
  "DWDB": "dw"
}
```

### Shared Artifacts Directory (`shared_artifacts_dir`)

A directory path for cross-package linked service and dataset deduplication. When multiple packages share the same connection managers, each shared artifact is written once.

```
C:\adf_output\shared
```

---

## 6. Quick Smoke Test

Use the synthetic FSI corpus, so you don't need any customer data:

1. Open Copilot Chat in Agent mode.
2. Run:

```
Scan for SSIS packages at samples/fsi/ssis and list what you find.
```

3. Then:

```
Analyze samples/fsi/ssis/ContosoBank.EOD/FIN_FX_DailyRates_Load.dtsx.
```

4. Then:

```
Convert it to work/demo/03_adf/FIN_FX_DailyRates_Load and validate the artifacts.
```

Next: follow [docs/CUSTOMER_FLOW.md](docs/CUSTOMER_FLOW.md) for the full analyze → refactor → test → reconcile → deploy flow on your own packages.

---

## 7. Development Workflow

```bash
# Run tests
pytest

# Lint
ruff check .

# Type-check
mypy ssis_adf_agent/

# Format (auto-fix)
ruff check --fix .
```

The project enforces `ruff` with `line-length = 100` and `mypy --strict`.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `ssis-adf-agent` command not found | Ensure the venv is activated, or use the full path to the script |
| `AttributeError: 'Server' object has no attribute 'list_tools'` | `mcp` 2.x is installed. Run `pip install -e .` again; the project pins `mcp<2` |
| Tools don't appear in Copilot Chat | Reload VS Code; confirm `.vscode/mcp.json` exists and Agent mode is selected |
| ODBC errors when scanning SQL Server | Install [ODBC Driver 17+](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) and verify with `odbcinst -j` (Linux) or ODBC Data Source Administrator (Windows) |
| `EncryptAllWithPassword` warnings | The SSIS package has encrypted connection strings. Passwords must be filled in manually in linked service JSON or referenced via Key Vault (`use_key_vault=true`) |
| LLM translation returns TODO stubs | Verify `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` are set. Check that the deployment name matches your Azure OpenAI resource. |
| `az login` required for deployment | Run `az login` before calling `deploy_to_adf`. For CI/CD, set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`. |
| Cross-DB references flagged as `manual_required` | Four-part names and `OPENQUERY`/`OPENROWSET` calls require architectural decisions — replace with elastic queries, external tables, or separate linked services. |
