---
mode: agent
tools:
  - analyze_ssis_package
  - validate_adf_artifacts
description: Work through a converted package's gaps one at a time and propose reviewed edits to the ADF artifacts (Phase 3 – refactor).
---

# Refactor conversion gaps

You are helping a migration engineer close the gaps that the deterministic converter could not handle.
Never mark a gap as resolved without showing the change and getting the user's confirmation.

## Inputs

- **Package path**: ${input:package_path:Absolute path to the original .dtsx file}
- **Artifacts directory**: ${input:artifacts_dir:Directory produced by convert_ssis_package, e.g. work/<migration>/03_adf/<Package>}

## Steps

1. Call `analyze_ssis_package` on the package. Gather every `manual_required` and `warning` gap.
2. Read the generated pipeline JSON under `pipeline/` and check it against the **pipeline review checklist** in `docs/CUSTOMER_FLOW.md`:
   - `dependsOn` matches the SSIS precedence constraints, including Failure, Completion, expression and OR paths. Read the `DTS:PrecedenceConstraint` elements in the .dtsx when you need to.
   - Every referenced data flow, dataset and linked service exists in the artifacts directory.
   - Variable types match the SSIS variable types.
   Add each problem you find to the gap list.
3. Present the full gap list as a table: id, task, gap, proposed approach, risk. Ask the user which gap to start with.
4. For each gap the user picks:
   - Explain what the SSIS logic does, quoting the relevant SQL, expression or C#.
   - Propose the smallest change to the ADF JSON, the Azure Function stub or a new SQL object that keeps the behaviour the same. Call out any semantic difference, such as null handling, decimal precision, ordering or fuzzy-match scores.
   - Make the edit only after the user approves it.
   - Re-run `validate_adf_artifacts`.
   - Record the decision in `work/<migration>/02_assessment/decisions.md`: the gap, the choice made, why, and who approved it.
5. End with an updated gap table and the G2 status: ready, or blocked by which gaps.

## Common refactor patterns

| SSIS pattern | Preferred ADF pattern |
|---|---|
| Script Task (C#) | Azure Function (Python) that keeps the ReadOnly/ReadWrite variable contract as the request/response |
| Linked server, `OPENQUERY`, four-part names | Separate source + Copy into staging, external table, or elastic query |
| SCD wizard / OLE DB Command | Set-based `MERGE` stored procedure, or Mapping Data Flow with Alter Row |
| Fuzzy Lookup | Document a redesign decision; never silently replace it with an exact join |
| Execute Process (gpg, WinSCP) | Azure Function / Batch job; SFTP linked service; keys in Key Vault |
| Sensitive parameters | Key Vault secret referenced from the linked service |
| Local or UNC paths | ADLS Gen2 / Blob paths with a parameterised dataset |
