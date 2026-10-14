---
mode: agent
description: Work through a converted package's gaps one at a time and propose reviewed edits to the ADF artifacts (Phase 3 – refactor).
---

# Refactor conversion gaps

You are helping a migration engineer close the gaps that the deterministic converter could not handle.
Never mark a gap as resolved without showing the change and getting the user's confirmation.

## Inputs

- **Workspace**: ${input:workspace:Migration workspace, e.g. work/<migration>}
- **Review item**: ${input:item_id:Open GAP-* or RECON-* item from the AI review queue}

## Steps

1. Run `ssis-adf review --workspace "<workspace>"` and find the requested item.
2. Read its original `.dtsx`, analysis JSON, and generated ADF artifacts. Check them against the **pipeline review checklist** in `docs/CUSTOMER_FLOW.md`:
   - `dependsOn` matches the SSIS precedence constraints, including Failure, Completion, expression and OR paths. Read the `DTS:PrecedenceConstraint` elements in the .dtsx when you need to.
   - Every referenced data flow, dataset and linked service exists in the artifacts directory.
   - Variable types match the SSIS variable types.
3. Explain the SSIS semantics, proposed implementation, risks, and tests for this item.
4. After the user approves the approach:
   - Explain what the SSIS logic does, quoting the relevant SQL, expression or C#.
   - Propose the smallest change to the ADF JSON, the Azure Function stub or a new SQL object that keeps the behaviour the same. Call out any semantic difference, such as null handling, decimal precision, ordering or fuzzy-match scores.
   - Make the edit only after the user approves it.
   - Run `ssis-adf review --workspace "<workspace>" --resolve "<item_id>" --decision "<decision>" --approved-by "<approver>"`.
   - Run `ssis-adf validate --workspace "<workspace>" --package "<package-key>"`.
5. Report whether G2 passed or which findings still block it.

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
