# Setup Guide

Step-by-step instructions for running the CLI-first migration workflow.

---

## 1. Prerequisites

| Requirement | Version | How to check | Install link |
|---|---|---|---|
| **Python** | 3.11+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| **pip** | Latest | `pip --version` | Included with Python |
| **Git** | Any | `git --version` | [git-scm.com](https://git-scm.com/) |
| **VS Code** | Latest stable | `code --version` | [code.visualstudio.com](https://code.visualstudio.com/) |
| **GitHub Copilot extension** | Agent support | Extensions sidebar → search "GitHub Copilot" | VS Code Marketplace |

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

# With the optional MCP compatibility adapter
pip install -e ".[mcp]"

# Everything
pip install -e ".[dev,llm,mcp]"
```

### Verify

```bash
python scripts/verify_install.py
```

This analyzes and converts a sample package through the deterministic engine without MCP or AI.

---

## 3. Kick Off a Migration

```powershell
ssis-adf migrate `
  --source samples\fsi\ssis\ContosoBank.EOD `
  --workspace work\demo
```

Open `work/demo/migration_report.md`, then ask Copilot Agent mode to review the open items in
`work/demo/02_assessment/ai_review_queue.json`. The deterministic engine already handled known
mappings; Copilot should focus only on uncertain logic and final semantic review.

Useful commands:

```powershell
ssis-adf status --workspace work\demo
ssis-adf review --workspace work\demo
ssis-adf gate --workspace work\demo --package FIN_FX_DailyRates_Load --gate G1 --result pass --evidence work\demo\migration_report.md --approved-by "Migration Owner"
ssis-adf validate --workspace work\demo --package FIN_FX_DailyRates_Load
```

## 4. Optional MCP Adapter

MCP is not required. To expose the legacy five tools to an MCP client, install `.[mcp]` and
register it explicitly:

```jsonc
{
  "github.copilot.chat.experimental.mcpServers": {
    "ssis-adf-agent": {
      "type": "stdio",
      "command": "ssis-adf-mcp",
      "args": []
    }
  }
}
```

---

## 5. Environment Variables

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

## 6. Optional Configuration Files

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

## 7. Quick Smoke Test

Use the synthetic FSI corpus, so you don't need any customer data:

1. Run the `ssis-adf migrate` command from section 3.
2. Read `work/demo/migration_report.md`.
3. Run `ssis-adf review --workspace work/demo`.
4. Run `ssis-adf validate --workspace work/demo --package FIN_FX_DailyRates_Load`.

Next: follow [docs/CUSTOMER_FLOW.md](docs/CUSTOMER_FLOW.md) for the full analyze → refactor → test → reconcile → deploy flow on your own packages.

---

## 8. Development Workflow

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
| `ssis-adf` command not found | Ensure the venv is activated and rerun `pip install -e .` |
| Optional MCP adapter fails to import | Install `pip install -e \".[mcp]\"`; the adapter pins `mcp<2` |
| Optional MCP tools don't appear | Reload VS Code, confirm `.[mcp]` is installed, and check your user-level MCP registration |
| ODBC errors when scanning SQL Server | Install [ODBC Driver 17+](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) and verify with `odbcinst -j` (Linux) or ODBC Data Source Administrator (Windows) |
| `EncryptAllWithPassword` warnings | The SSIS package has encrypted connection strings. Passwords must be filled in manually in linked service JSON or referenced via Key Vault (`use_key_vault=true`) |
| LLM translation returns TODO stubs | Verify `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` are set. Check that the deployment name matches your Azure OpenAI resource. |
| `az login` required for deployment | Run `az login` before `ssis-adf deploy`. For CI/CD, set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`. |
| Cross-DB references flagged as `manual_required` | Four-part names and `OPENQUERY`/`OPENROWSET` calls require architectural decisions — replace with elastic queries, external tables, or separate linked services. |
