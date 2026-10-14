---
applyTo: "**"
---

# SSIS → ADF Migration Workflow — Copilot Domain Knowledge

## Project Overview
This project is a CLI-first, AI-orchestrated SSIS migration workflow. The deterministic
Python engine handles inventory, analysis, known SSIS-to-ADF mappings, artifact generation,
validation, and workflow gates without using AI tokens. Copilot handles only uncertain
logic, semantic review, and reconciliation mismatches.

Start a migration with:

```powershell
ssis-adf migrate --source <ssis-folder> --workspace work/<migration-name>
```

Then read:

- `migration_report.md` for the customer summary.
- `02_assessment/ai_review_queue.json` for work that actually needs AI.
- The original `.dtsx` and generated `03_adf/<package>/` artifacts when investigating a finding.

## Copilot operating rules

1. Do not re-convert obvious mappings or spend model reasoning on items absent from the review queue.
2. Work through open review items one at a time and preserve the original SSIS behavior.
3. Explain semantic differences involving nulls, ordering, precision, joins, error paths, or dropped rows.
4. Record an approved decision with `ssis-adf review`; never silently mark an item resolved.
5. Run `ssis-adf validate` after edits. G2 cannot pass while review items or validation errors remain.
6. Treat final semantic review as risk analysis, not proof of equivalence.
7. Reconciliation is proof. Record its evidence with `ssis-adf reconcile`.
8. When reconciliation fails, diagnose the new `RECON-*` queue item, refactor, validate, test, and reconcile again.

MCP remains an optional adapter for compatible clients. Install `.[mcp]` and run
`ssis-adf-mcp` only when explicitly desired; do not require MCP for the customer workflow.

## SSIS Component Mapping

| SSIS Component | ADF Equivalent | Notes |
|---|---|---|
| Execute SQL Task | Stored Procedure / Script / Lookup Activity | Depends on ResultSetType and SQL pattern |
| Data Flow Task (simple) | Copy Activity | Single source → single destination, no transformations |
| Data Flow Task (complex) | Execute Data Flow Activity (Mapping Data Flow) | Multiple sources, transformations, or fanout |
| Execute Package Task | Execute Pipeline Activity | Child pipeline must also be converted |
| Script Task (C# / VB) | Azure Function Activity | Stub generated at `stubs/<FunctionName>/__init__.py`; requires manual porting |
| ForEach Loop Container | ForEach Activity | Expression varies by enumerator type |
| For Loop Container | SetVariable (init) + Until Activity + SetVariable (increment) | Condition logic is inverted |
| Sequence Container | Flattened into parent with dependsOn chaining | No ADF equivalent |
| File System Task | Copy Activity (Azure paths) or Web Activity → Azure Function | Local paths need Azure-path substitution |
| Execute Process Task | Web Activity → Azure Function | Manual: wrap process call in Function |
| FTP Task | FTP connector via Copy Activity | Requires FTP linked service |
| Send Mail Task | Logic App / Web Activity | No native ADF equivalent |
| Event Handler (OnError) | Pipeline fails path / error handling | Converted to sub-pipeline reference |
| Event Handler (OnPostExecute) | Succeeded dependency path | Converted to sub-pipeline reference |

---

## ADF Artifact Structure

Output directories written by `convert_ssis_package`:

```
<output_dir>/
  pipeline/           ← <PackageName>.json
  linkedService/      ← one per ConnectionManager
  dataset/            ← one per data flow source / destination
  dataflow/           ← one per complex Data Flow Task
  trigger/            ← ScheduleTrigger template (Stopped state)
  stubs/              ← Azure Function stubs for Script Tasks
    <FunctionName>/
      __init__.py     ← Python stub with TODO blocks
      function.json
```

---

## Key Domain Rules

1. **Triggers are ALWAYS deployed in Stopped state** — activate them manually after validating pipelines.
2. **Script Task stubs require manual work** — the generated Python stub preserves the original C# variable interface as TODO comments.
3. **Encryption warnings** — packages with `EncryptAllWithPassword` protection level may have missing connection string passwords.
4. **SSIS variables syntax** — `@[User::VarName]` / `@(Ns::VarName)` → `@variables('VarName')` in ADF expressions.
5. **For Loop condition inversion** — SSIS loop `InitExpression` runs once; `EvalExpression` is true-while-looping (opposite of ADF Until's doWhileCondition).  The converter negates the expression.
6. **Sequence containers are flattened** — tasks inside are promoted to the parent with `dependsOn` chaining.
7. **Precedence constraints** — Expression-type constraints produce ADF expressions on `dependsOn`; conjunctions (AND) vs. disjunctions (OR) are resolved via the dependency graph.
8. **Re-deployment** — already-existing linked services / datasets are overwritten (put_or_update semantics).

---

## Authentication for Deployment

`deploy_to_adf` uses `DefaultAzureCredential` in this order:
1. Environment variables (`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`)
2. Workload identity
3. Azure CLI (`az login`)
4. Azure PowerShell

Run `az login` before deploying if using a developer machine.

---

## Complexity Score Guide

| Score | Label | Typical Effort |
|---|---|---|
| 0–25 | Low | < 1 day |
| 26–50 | Medium | 1–3 days |
| 51–75 | High | 3–5 days |
| 76–100 | Very High | 1+ weeks |

Score drivers: Script Tasks (+20 each), Data Flow Tasks (+8 each), ForEach/ForLoop (+5 each), Unknown tasks (+10 each).

---

## Python Package Layout

```
ssis_adf_agent/
  mcp_server.py          ← MCP entry point (this is the stdio server)
  parsers/
    models.py            ← Pydantic IR models
    ssis_parser.py       ← .dtsx XML parser
    readers/
      local_reader.py
      git_reader.py
      sql_reader.py
  analyzers/
    complexity_scorer.py
    gap_analyzer.py
    dependency_graph.py
  converters/
    dispatcher.py
    base_converter.py
    control_flow/        ← 8 converters
    data_flow/           ← 3 converters
  generators/            ← 5 generators (pipeline, linked_service, dataset, dataflow, trigger)
  deployer/
    adf_deployer.py
```
