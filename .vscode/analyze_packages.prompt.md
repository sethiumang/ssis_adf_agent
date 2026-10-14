---
mode: agent
description: Kick off deterministic inventory, analysis, conversion, and AI triage for SSIS packages.
---

# Analyze SSIS Packages

Start the migration workflow. The deterministic engine must handle known patterns without
spending AI reasoning. Use AI only for the generated review queue.

## Inputs

- **Source folder**: ${input:source:Local directory containing .dtsx packages}
- **Workspace**: ${input:workspace:Migration workspace, e.g. work/customer-name}

## Steps

1. Run `ssis-adf migrate --source "<source>" --workspace "<workspace>"`.
2. Read `<workspace>/migration_report.md`.
3. Read `<workspace>/02_assessment/ai_review_queue.json`.
4. Summarize the migration waves and open findings, grouped as:
   - deterministic conversion complete,
   - review required,
   - AI refactor required,
   - architecture decision required.
5. Do not refactor anything until the user selects or approves a review item.
