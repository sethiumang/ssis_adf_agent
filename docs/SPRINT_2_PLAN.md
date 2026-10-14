# Sprint 2 plan: customer-ready MVP

**Goal:** a customer clones the repo, converts a package, and can prove the result matches SSIS for at least one end-to-end example.

**Duration:** about 2 weeks. **Status markers:** `[ ]` open, `[x]` done.

## Track A: Engine correctness (must)

Fix the gaps found by `samples/fsi/tools/validate_corpus.py` (see `samples/fsi/README.md`, findings P1–P12).

- [x] A1 (P1) Resolve precedence-constraint `From`/`To` refIds to tasks, so `dependsOn` is generated
- [x] A2 (P2) Detect `LogicalAnd="False"` (OR)
- [x] A3 (P3) Correct the `EvalOp` mapping (1 = Expression, 2 = Constraint, 3 = both, 4 = either)
- [ ] A4 (P5) Generate data flows for Data Flow tasks nested in containers
- [ ] A5 (P6) Generate `LS_AzureFunction` when any activity references it
- [ ] A6 (P7) `validate_artifacts` reports dangling data flow, dataset and linked service references
- [ ] A7 (P9) Parse variable data types
- [ ] A8 (P4) Recognise `Microsoft.*` component class names
- [ ] A9 (P10) Generate datasets for data-flow sources and sinks
- [ ] A10 (P8) Recognise Execute Process Task
- [ ] A11 Convert `validate_corpus.py` checks into pytest regression tests, run in CI

## Track B: SSIS baseline (must, 3 packages)

Packages 01 FX (simple), 02 GL incremental (watermark, OR path), 03 Custodian (folder loop, files, planted dropped-rows trap).

- [ ] B1 Windows VM with SQL Server 2022 Developer edition, SSIS, Visual Studio 2022 with the SSIS Projects extension
- [ ] B2 Open all 10 packages in Visual Studio; fix the generator if any fail to load. Recompile Script Tasks
- [ ] B3 Scripted DDL, seed data and input files for packages 01–03 (`samples/fsi/data/`)
- [ ] B4 Deploy to SSISDB and run for a fixed business date
- [ ] B5 Capture goldens (row counts, control totals, row hashes, file hashes) into `samples/fsi/baseline/`

## Track C: One full golden path (must)

- [ ] C1 Sandbox resource group: Data Factory, Azure SQL DB, storage account
- [ ] C2 Take package 01 through all six phases from `docs/CUSTOMER_FLOW.md`
- [ ] C3 Produce the evidence pack (test runs, reconciliation, sign-off) as the customer template

## Track D: Packaging (should)

- [ ] D1 GitHub Actions: ruff, pytest, `generate_packages.py --check`, `validate_corpus.py`
- [ ] D2 Version tag and CHANGELOG
- [ ] D3 Support statement and issue templates
- [ ] D4 Replace the `<this-repo-url>` placeholder; confirm the license copyright holder

## Order of work

1. Days 1–2: VM setup, open packages in Visual Studio (B1–B2); start A1–A3
2. Days 3–6: A4–A11; B3 in parallel
3. Days 7–8: B4–B5
4. Days 9–10: C1–C3, D1–D4

## Definition of done

- A fresh clone passes `python scripts/verify_install.py`
- Every converted test package has correct `dependsOn` and no dangling references
- One package has a full evidence pack showing ADF matches SSIS
- CI is green
