# FSI SSIS sample corpus: Contoso Bank EOD

This folder holds 10 synthetic SSIS packages modelled on real financial-services (FSI) batch workloads. Use them to test SSIS → ADF migration tooling.

They are fictional. "Contoso Bank", the hosts, schemas and file layouts are all made up, and no customer data or code is included.

This is Sprint 1 of the test-corpus plan. See [Sprint plan](#sprint-plan) for what comes next.

```
samples/fsi/
  catalog.json                    ← per-package metadata + ground-truth inventory (test oracle)
  ssis/ContosoBank.EOD/           ← SSIS 2019 project-deployment-model packages
    Project.params
    01..10 *.dtsx
  tools/
    dtsx_builder.py               ← deterministic DTSX writer (stable GUIDs/lineage IDs)
    common.py                     ← shared connection managers, params, audit pattern
    packages_a.py, packages_b.py  ← the 10 package definitions + scenario metadata
    generate_packages.py          ← regenerate (or --check) the corpus
    validate_corpus.py            ← integrity + parser-parity + conversion probe
```

## Why these scenarios

We ran desk research on how banks, broker-dealers and asset managers still use SSIS today. The packages are built around those recurring patterns:

| Theme | What FSI teams typically build in SSIS | Sources |
|---|---|---|
| Regulatory data aggregation | Nightly ETL into a risk/finance mart feeding BCBS 239 aggregation and FR Y‑14Q schedules | [BIS BCBS 239](https://www.bis.org/publ/bcbs239.htm), [FR Y‑14Q](https://www.federalreserve.gov/apps/reportingforms/Report/Index/FR_Y-14Q) |
| AML / BSA | Daily cash aggregation for Currency Transaction Reports (> $10,000), exemption handling, BSA E‑Filing worklists | [31 CFR 1010.311](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-C/section-1010.311), [FinCEN BSA E‑Filing](https://bsaefiling.fincen.treas.gov/) |
| Sanctions | Daily download of OFAC SDN list and fuzzy name screening | [OFAC Sanctions List Service](https://ofac.treasury.gov/sanctions-list-service) |
| Payments | Building fixed-width NACHA ACH files, PGP encryption, SFTP delivery to the ODFI | [Nacha Operating Rules](https://www.nacha.org/rules) |
| Reference data | FX rates (H.10-style) loaded daily for revaluation | [Federal Reserve H.10](https://www.federalreserve.gov/releases/h10/) |
| Ops / custody | Custodian position files picked up by folder loop, validated, quarantined | — |
| Treasury | Bank statement (BAI2 / camt.053) to GL reconciliation with tolerance | — |
| Customer master | SCD Type 2 customer/KYC dimensions | [SSIS SCD transformation](https://learn.microsoft.com/sql/integration-services/data-flow/transformations/slowly-changing-dimension-transformation) |
| Orchestration | A master package driven by a SQL Agent job, calling child packages with Execute Package Task | [SSIS overview](https://learn.microsoft.com/sql/integration-services/sql-server-integration-services), [Foreach Loop](https://learn.microsoft.com/sql/integration-services/control-flow/foreach-loop-container) |

We also used Microsoft's [SSIS → Azure migration overview](https://learn.microsoft.com/azure/data-factory/scenario-ssis-migration-overview) to choose features that are hard to migrate.

## Package catalog

The task, container and component counts come from the generated XML. The full detail is in `catalog.json`.

| # | Package | Domain | Regulatory context | Tasks / containers / DF components |
|---|---|---|---|---|
| 01 | `FIN_FX_DailyRates_Load` | Finance – reference data | GL revaluation, BCBS 239 common currency | 6 / 0 / 2 |
| 02 | `FIN_GL_JournalEntries_Incremental` | Finance – GL | Financial statements, Call Report, SOX | 7 / 0 / 4 |
| 03 | `OPS_Custodian_Positions_Ingest` | Investment ops | SEC 17a‑13 style position recon | 7 / 1 / 9 |
| 04 | `CRM_Customer_KYC_SCD2` | Customer / KYC | CDD rule (31 CFR 1010.230), GLBA PII | 4 / 1 / 10 |
| 05 | `AML_CTR_Daily_Monitoring` | AML | CTR > $10k, exemptions, BSA E‑Filing | 8 / 0 / 10 |
| 06 | `PAY_ACH_NACHA_Outbound` | Payments | Nacha file format, fraud monitoring | 11 / 1 / 3 |
| 07 | `TRS_Bank_Statement_Reconciliation` | Treasury | SOX cash controls, BCBS 248 | 7 / 0 / 12 |
| 08 | `RISK_Credit_Exposure_Aggregation` | Credit risk | BCBS 239, FR Y‑14Q, large exposures | 6 / 0 / 13 |
| 09 | `CMP_OFAC_Sanctions_Screening` | Sanctions | OFAC (31 CFR Ch. V), list-version evidence | 8 / 0 / 12 |
| 10 | `ORCH_EOD_Regulatory_Batch_Master` | Orchestration | Reporting SLAs, BCBS 239 timeliness | 17 / 4 / 0 |

## Feature coverage

| SSIS feature | Packages |
|---|---|
| Project and package parameters, sensitive parameter | all, 06 |
| Variable / property expressions, dynamic connection strings | 01, 03, 05, 06, 07, 10 |
| Execute SQL: None / SingleRow / Full result set, OUTPUT parameters, MERGE, THROW | all |
| Linked server `OPENQUERY`, four-part and cross-database names | 08 |
| Foreach File, Foreach ADO enumerators | 03, 06 |
| For Loop (polling with WAITFOR) | 10 |
| Sequence containers, parallel branches | 04, 10 |
| Execute Package Task (project reference, parameter assignments) | 10 |
| Precedence: Success / Failure / Completion, expression, OR fan-in | 02, 03, 07, 10 |
| Event handlers (OnError) | 05, 09, 10 |
| Script Task (C#), Script Component (C#) | 05, 06, 09 / 06 |
| Execute Process (gpg, WinSCP), File System, Send Mail | 06 / 03, 06 / 05, 07, 09, 10 |
| Derived Column, Conditional Split, Lookup (+ no-match), Multicast, Union All, Row Count | 02–09 |
| Aggregate, Sort + Merge Join (full outer) | 05, 07, 08 / 07 |
| SCD wizard, OLE DB Command | 04 |
| Fuzzy Lookup (Enterprise edition) | 09 |
| ProtectionLevel = EncryptSensitiveWithUserKey | 06 |

### Planted edge cases

These are deliberate traps that a migration and reconciliation tool should catch:

- **03:** the "Zero Quantity" Conditional Split output goes nowhere, so those rows are silently dropped.
- **05:** the "Below Threshold" output is unused, and the threshold is a decimal project parameter.
- **06:** the output must be byte-exact (NACHA), so row counts alone are not enough to reconcile it. The package also depends on local executables and reads a sensitive parameter in script.
- **07:** Sort/Merge Join null semantics, and a many-to-many join on duplicate references.
- **08:** the FX lookup uses `MAX(RateDate)` instead of the business date (a latent bug), and no-match rows are diverted to a side table.
- **09:** Fuzzy Lookup scores will differ after migration, so reconcile the *alert sets*, not counts.
- **10:** the For Loop condition must be negated for ADF Until, and a Completion constraint means a payments failure does not block the close.

## Regenerate and validate

From the repo root, with the package installed (`pip install -e ".[dev]"`):

```powershell
python samples\fsi\tools\generate_packages.py           # rewrite .dtsx, Project.params, catalog.json
python samples\fsi\tools\generate_packages.py --check   # CI: fail if committed files are stale
python samples\fsi\tools\validate_corpus.py --json report.json --keep-output out\
```

Generation is deterministic: GUIDs are uuid5 values derived from object paths. To change a package, edit `packages_a.py` or `packages_b.py`, never the `.dtsx` directly.

`validate_corpus.py` runs three layers:

1. **Corpus integrity.** Is the XML well-formed, and does every reference resolve? This covers precedence `From`/`To`, path start/end, lineage IDs, `InputColumnID`, connection managers, Execute SQL connections, and `User::` variables. It also catches inputs that are never connected and duplicate refIds. The script exits non-zero if this layer fails.
2. **Parser parity.** It compares the repo's `SSISParser` output with the ground-truth `inventory` in `catalog.json`.
3. **Conversion probe.** It runs complexity scoring, gap analysis, all generators and `validate_artifacts`. It also checks for dangling ADF references, meaning references to data flows or linked services that were never generated.

## Sprint 1 results (current `main` engine)

All 10 packages pass corpus integrity, and regeneration is byte-identical. Running them through the current engine shows the gaps below. They are recorded here as a backlog and have **not** been fixed.

| # | Finding | Impact | Evidence |
|---|---|---|---|
| P1 | Precedence constraints are not linked to tasks. SSIS 2012+ writes `DTS:From`/`DTS:To` as refIds (`Package\Task`), but the parser matches them against DTSIDs. | **Every converted pipeline has empty `dependsOn`**, so all activities would run in parallel. | 0 of 68 constraints resolve |
| P2 | `LogicalAnd="False"` (OR) is not detected. | OR fan-in becomes AND. | packages 02, 07, 10 |
| P3 | The `EvalOp` mapping is off by one. SSIS uses 1 = Expression and 2 = Constraint. | Expression constraints are treated as plain constraints, and the other way round. | `ssis_parser.py` constraint parsing |
| P4 | Data-flow components are keyed only by legacy CLSIDs, so the `Microsoft.*` class names used by SSIS 2012+ are not recognised. | Component type falls back to the raw class name, and transformation mapping degrades. | all packages |
| P5 | Data flows nested inside containers are not generated. `generate_data_flows` only walks top-level tasks. | The pipeline references `DF_*` data flows that don't exist, so deployment fails. | 03, 04, 06 |
| P6 | Azure Function activities reference `LS_AzureFunction`, but that linked service is never generated. | Deployment fails. | 05, 06, 09 |
| P7 | `validate_artifacts` reports "valid" even when P5/P6 dangling references exist. | False confidence in the output. | all of the above |
| P8 | Execute Process Task is reported as UNKNOWN. | It is not routed to a Function or Batch pattern. | 06 |
| P9 | User variable data types are lost; everything becomes String. | Wrong ADF variable types, and broken numeric/date comparisons. | all packages |
| P10 | No datasets are generated for any data-flow source or sink. | Copy and Data Flow activities can't run. | all packages |
| P11 | `mcp_server` fails to import with `mcp` 2.x (`Server.list_tools` was removed), because `pyproject.toml` pins only `mcp>=1.0.0`. | The MCP server breaks on a fresh install. | fresh `pip install -e .` (mcp 2.2.0) |
| P12 | Planted semantic traps (unconnected outputs, `MAX(RateDate)`, Fuzzy Lookup, byte-exact NACHA) are not flagged by the gap analyzer. | These need reconciliation and evidence gates, not just conversion. | 03, 08, 09, 06 |

These findings are what the [AI migration platform plan](../../docs/AI_MIGRATION_PLATFORM_PLAN.md) relies on for its trusted converter and reconciliation gates.

## Fidelity caveat

The packages follow the SSIS 2019 (PackageFormatVersion 8) DTSX schema and pass our structural checks. However, they have **not yet been opened in SSDT / Visual Studio or executed**. The Script Task and Script Component projects contain real C# source but no compiled binaries. Sprint 2 closes this gap.

## Sprint plan

| Sprint | Scope | Exit criteria |
|---|---|---|
| **1 – Package corpus** (done) | 10 FSI packages, catalog, generator, validator, findings | Integrity passes; findings recorded |
| 2 – SSIS fidelity | Open/save in SSDT, `.dtproj`/`.ispac`, SSISDB environments, SQL Agent job scripts, a 2008-format variant, extra edge cases (event-handler nesting, disabled tasks, checkpoints) | Packages validate in SSDT; ispac builds |
| 3 – Data & schemas | DDL for CoreBanking/FinanceDW/ETLControl and the other source databases; seed data; input files (H.10 CSV, custodian, BAI2/camt.053, NACHA, SDN) | `deploy-db` script stands up all databases locally |
| 4 – SSIS baseline | Execute packages on SQL Server / SSIS IR, capture golden outputs (row counts, hashes, files) | Golden outputs committed |
| 5 – Test harness | Parse/inventory golden tests, conversion snapshot tests, ADF run and reconciliation against Sprint 4 goldens | CI gate on corpus |
