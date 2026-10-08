---
mode: agent
tools:
  - analyze_ssis_package
description: Generate reconciliation queries and a sign-off template that prove an ADF pipeline produces the same data as its SSIS package (Phase 5 – reconcile).
---

# Reconcile an SSIS package against its ADF pipeline

## Inputs

- **Package path**: ${input:package_path:Absolute path to the original .dtsx file}
- **Output folder**: ${input:recon_dir:Folder for reconciliation assets, e.g. work/<migration>/05_recon/<Package>}
- **How the two outputs are separated**: ${input:separation:e.g. SSIS writes to FinanceDW, ADF writes to FinanceDW_ADF; or schema ssis vs adf}

## Steps

1. Call `analyze_ssis_package`. From the data flows and Execute SQL tasks, list every **target**:
   - tables written to by destinations, `INSERT`/`MERGE`/`UPDATE` statements,
   - reject and side tables,
   - files written by flat-file destinations.
   For each target, name the business key and the business date column.
2. Flag the **semantic risks** a plain row count would miss:
   - Conditional Split outputs that are not connected (rows silently dropped)
   - Lookups with no-match outputs
   - Sort / Merge Join (null semantics, duplicate keys)
   - Fuzzy Lookup (scores will differ)
   - Decimal or FX calculations
   - Fixed-width files (byte-exact output)
   - Alert-producing logic (AML, sanctions)
3. Write `recon.sql` into the output folder. For each target table, include parameterised T-SQL for:
   - row count per business date,
   - control totals (SUM of amount/quantity columns, grouped by currency or account where present),
   - key-set difference in both directions (`EXCEPT`),
   - a row-hash difference (`HASHBYTES('SHA2_256', CONCAT_WS('|', ...))`) over the business columns.
   For **files**, write a PowerShell snippet that compares length and SHA-256.
   For **alert logic**, compare the sets of flagged IDs, not counts.
4. Write `signoff.md` containing:
   - a results table (check, SSIS value, ADF value, difference, tolerance, pass/fail),
   - the agreed tolerances,
   - the semantic risks from step 2 and how each was verified,
   - data owner name, date and decision.
5. Tell the user to run both systems on the **same input** for the same business date, paste the results into `signoff.md`, and ask the data owner to sign off (gate G4).

Do not claim the package is reconciled. Only the results the user pastes in, and the data owner's sign-off, establish that.
