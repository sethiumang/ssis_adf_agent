---
mode: agent
description: Run the deterministic-first migration workflow and report the generated AI review queue.
---

# Convert SSIS Package to ADF

Convert SSIS packages through the CLI-first workflow. Do not use AI to recreate mappings
already produced by the deterministic engine.

## Parameters

- **Source folder**: ${input:source:Folder containing the .dtsx package}
- **Workspace**: ${input:workspace:Migration workspace, e.g. work/customer-name}

## Steps

1. Run `ssis-adf migrate --source "<source>" --workspace "<workspace>"`.
2. Present `migration_report.md`.
3. Present open items from `02_assessment/ai_review_queue.json`.
4. Explain that generated ADF files are a deterministic baseline, not proof of equivalence.
5. Ask which open finding the user wants reviewed first.
