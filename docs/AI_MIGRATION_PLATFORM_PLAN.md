# AI-Powered SSIS to ADF Migration Platform Plan

## Executive summary

The goal is to evolve this repository from a package conversion utility into an
AI-powered migration platform that helps customers discover, understand, convert,
test, reconcile, and operationalize SSIS workloads in Azure Data Factory (ADF).

Customers should interact with a migration agent in natural language, while a
deterministic Python engine performs parsing, conversion, validation, deployment,
and reconciliation. AI accelerates interpretation and remediation, but production
readiness is established through explicit rules, test evidence, and human approval.

The product promise is:

> An AI-powered SSIS migration factory that discovers, converts, tests, reconciles,
> and documents migrations to ADF with traceable decisions and evidence-based
> production readiness.

## Product principles

1. **Agent-guided, not prompt-dependent.** A conversation starts and guides a
   persistent migration project; migration logic does not live in a prompt.
2. **Deterministic at the core.** The same package, configuration, and rule-set
   version must produce the same inventory and generated artifacts.
3. **Evidence over confidence.** Readiness is based on coverage, validation, and
   reconciliation results rather than a single opaque AI confidence score.
4. **AI with bounded authority.** AI can explain, propose, translate, and diagnose.
   It cannot silently accept behavioral differences or approve production deployment.
5. **No false success.** Unsupported logic, placeholders, unresolved connections,
   and incomplete translations are blocking findings unless explicitly accepted.
6. **Traceability by default.** Every generated artifact can be traced to source
   packages, components, conversion rules, assumptions, and approvals.
7. **Secure by design.** Inventory and reports contain secret references, never
   exported credentials or sensitive connection values.
8. **Reusable interfaces.** The same application services support an agent, CLI,
   API, batch workflows, and CI/CD.

## Customer experience

### Starting a migration

A customer installs the solution locally, deploys it into their Azure environment,
or uses a hosted deployment. They point it to one of these supported sources:

- A local directory containing SSIS projects and packages
- A Git repository
- SSISDB
- MSDB and SQL Agent metadata

They can begin with a natural-language request:

> Analyze the SSIS packages in `C:\CustomerETL`, create an inventory, and recommend
> a migration plan for our development ADF environment.

The agent collects only the missing information, creates a persistent migration
project, and invokes deterministic workflow operations.

### End-to-end flow

```text
Customer
   |
   v
AI Migration Agent
   |
   v
Create migration project and immutable source snapshot
   |
   v
Discover -> Parse -> Inventory -> Assess
   |
   v
Review conversion plan and resolve required decisions
   |
   v
Generate ADF assets and reconciliation specifications
   |
   v
Static validation and policy gates
   |
   v
Deploy to staging with triggers stopped
   |
   v
Run SSIS and ADF against equivalent inputs
   |
   v
Reconcile outputs and diagnose differences
   |
   v
Approve exceptions and publish readiness report
   |
   v
Controlled production deployment
```

### Customer deliverables

Each migration project produces:

1. **Estate inventory** covering packages, projects, jobs, schedules, dependencies,
   connections, variables, SQL objects, components, and lineage.
2. **Assessment report** covering complexity, support classification, risks,
   assumptions, decisions, and estimated effort.
3. **Conversion plan** describing the selected ADF implementation for each SSIS
   component.
4. **ADF assets** including pipelines, linked services, datasets, data flows,
   triggers, parameters, and supporting code.
5. **Reconciliation suite** containing generated comparison rules and execution
   configuration.
6. **Migration evidence report** containing validation results, test outcomes,
   accepted exceptions, approvals, and release readiness.

## Solution architecture

```text
Interfaces
  +-- AI agent through MCP
  +-- CLI
  +-- REST API
  +-- Web portal
  +-- CI/CD integration
          |
          v
Application and workflow services
  +-- Migration project service
  +-- Job orchestration and resumability
  +-- Decision and approval service
  +-- Policy and readiness gates
          |
          v
Deterministic migration engine
  +-- Source adapters
  +-- SSIS parser and canonical IR
  +-- Inventory and lineage analysis
  +-- Conversion capability registry
  +-- Conversion planners and generators
  +-- Static validators
  +-- ADF deployment adapter
  +-- Execution and reconciliation engine
          |
          v
Persistent project store and artifact store
  +-- Source snapshots
  +-- Versioned IR
  +-- Generated assets
  +-- Decisions and approvals
  +-- Run history and reconciliation evidence
```

### Agent

The agent is the primary guided experience. It:

- Interprets the customer's goal
- Creates and resumes migration projects
- Collects missing configuration
- Explains packages and findings
- Proposes architecture and remediation options
- Invokes deterministic workflow operations
- Diagnoses conversion and reconciliation failures
- Presents decisions for human approval

The agent does not contain the conversion rules. MCP tools expose workflow
capabilities to the agent.

### Deterministic engine

The deterministic engine is ordinary versioned Python code. It:

- Parses DTSX and deployment metadata
- Creates the canonical SSIS intermediate representation (IR)
- Applies explicit mapping and policy rules
- Generates repeatable ADF artifacts
- Validates references, expressions, and configurations
- Deploys approved artifacts
- Executes reconciliation rules
- Produces machine-readable evidence

The current `parsers`, `analyzers`, `converters`, `generators`, and `deployer`
packages form the beginning of this engine.

### Workflow service

The workflow service turns individual operations into a durable migration process.
It records state, supports retries and resumption, and prevents later stages from
running when required gates have failed.

Suggested workflow states:

```text
CREATED
DISCOVERED
ASSESSED
AWAITING_DECISIONS
APPROVED_FOR_CONVERSION
CONVERTED
STATIC_VALIDATION_FAILED
READY_FOR_STAGING
STAGING_DEPLOYED
RECONCILIATION_FAILED
READY_WITH_EXCEPTIONS
READY_FOR_PRODUCTION
PRODUCTION_DEPLOYED
```

## Role of AI

### High-value AI capabilities

AI should accelerate work that requires interpretation or generation:

- Summarize unfamiliar packages and explain business flow
- Infer likely intent from SQL, expressions, names, and topology
- Recommend Copy Activity, Mapping Data Flow, stored procedure, notebook,
  Azure Function, or redesign
- Translate Script Tasks into draft Azure Functions or notebooks
- Rewrite incompatible SQL into an Azure-compatible draft
- Identify duplicate logic and consolidation opportunities across an estate
- Propose connection and parameter mappings
- Generate reconciliation rules from observed package behavior
- Diagnose failed ADF runs and reconciliation differences
- Produce technical and customer-facing migration documentation

### AI boundaries

AI must not independently:

- Handle or reproduce secrets
- Activate production triggers
- Execute destructive SQL
- Approve reconciliation differences
- Replace unsupported behavior without disclosure
- Declare semantic equivalence
- Approve production deployment

AI-generated changes must include source context, assumptions, model metadata,
and an approval or reconciliation requirement.

## Canonical intermediate representation

The IR is the contract between ingestion, analysis, conversion, and reporting.
It should be versioned independently from the source format and include:

- Projects, packages, folders, jobs, schedules, and environments
- Control-flow tasks and nested containers
- Precedence constraints, expressions, and event handlers
- Data-flow components, ports, paths, column mappings, and error outputs
- Variables, parameters, configurations, and sensitive-value metadata
- Connection managers and external dependencies
- SQL statements, stored procedures, tables, files, and endpoints
- Source-to-target lineage
- Parser diagnostics and unparsed source evidence
- Stable source identifiers and source locations

Missing or unparsed behavior must be represented explicitly. The parser must not
invent identifiers or silently discard components.

## Conversion capability registry

Every supported component should have a versioned capability definition:

| Field | Purpose |
|---|---|
| SSIS component and version | Identifies the source behavior |
| Conversion classification | Exact, equivalent, approximate, manual, unsupported |
| ADF implementation | Target activity or external service |
| Rule version | Makes generated behavior reproducible |
| Preconditions | Conditions required for the rule to apply |
| Assumptions | Behavior that must be confirmed |
| Required validations | Static and runtime checks |
| Reconciliation strategy | Evidence required for acceptance |

Suggested classifications:

- **Exact**: deterministic mapping with equivalent semantics
- **Equivalent**: different implementation with a defined equivalence contract
- **Approximate**: likely mapping that requires reconciliation and approval
- **Manual**: generated scaffold or recommendation requiring implementation
- **Unsupported**: no approved migration path

Unsupported and manual components must never be replaced by success-shaped
placeholder activities.

## Trust and readiness model

Do not represent trust as one aggregate confidence score. Report separate,
auditable dimensions:

| Dimension | Example evidence |
|---|---|
| Parse coverage | Percentage of executable components represented in the IR |
| Conversion coverage | Exact, equivalent, approximate, manual, unsupported counts |
| Static validity | ADF schemas, references, expressions, and dependencies resolve |
| Deployment validity | Assets compile and deploy successfully in staging |
| Behavioral equivalence | Reconciliation checks passed for equivalent inputs |
| Operational readiness | Secrets, monitoring, retries, triggers, and rollback configured |

Every generated artifact should include traceability metadata comparable to:

```json
{
  "sourcePackage": "LoadSales.dtsx",
  "sourceComponentId": "source-id",
  "conversionRule": "data-flow.simple-copy.v2",
  "conversionClass": "exact",
  "assumptions": [],
  "evidence": [
    "source-query",
    "column-map",
    "precedence-constraint"
  ]
}
```

Suggested final readiness states:

- `READY`
- `READY_WITH_ACCEPTED_EXCEPTIONS`
- `RECONCILIATION_FAILED`
- `BLOCKED_MANUAL_WORK`
- `UNSUPPORTED`

## Reconciliation

Reconciliation is part of conversion, not a reporting feature added afterward.
The analysis stage should generate a reconciliation specification for each
package and important data boundary.

### Comparison types

- Schema, datatype, precision, and nullability comparison
- Source, read, written, rejected, inserted, updated, and deleted row counts
- Key uniqueness and duplicate counts
- Null counts and column-level profiles
- Minimum, maximum, sum, and configured business aggregates
- Deterministic row hashes for appropriate datasets
- Key-set comparison for large datasets
- Watermark and incremental-window validation
- Reject and error-path comparison
- Control-flow branch and dependency outcome comparison
- Runtime, throughput, and SLA comparison

### Execution approach

SSIS and ADF should run against the same immutable input or logically equivalent
input window. Results should capture:

- Source package and artifact versions
- Rule-set and AI-assistance versions
- Environment and parameter values
- Input snapshot or watermark
- Query and comparison definitions
- Tolerances
- Execution timestamps
- Detailed differences
- Approval and exception history

Reconciliation tolerances must be explicit and version-controlled.

## Migration project manifest

Customers should be able to configure a project using a version-controlled
manifest rather than a long list of command arguments:

```yaml
apiVersion: ssis-adf-agent/v1
project: customer-finance-etl

source:
  type: filesystem
  location: C:\SSIS\Finance

target:
  subscription: ${AZURE_SUBSCRIPTION_ID}
  resourceGroup: rg-data-dev
  factory: adf-finance-dev
  integrationRuntime: SelfHostedIR

policies:
  allowApproximateConversions: false
  failOnUnsupported: true
  deployTriggersStopped: true
  minimumReconciliationPassRate: 1.0

mappings:
  connections: ./config/connections.yaml
  schemas: ./config/schema-map.yaml
  secrets: ./config/secret-references.yaml

reconciliation:
  defaultMode: aggregates
  tolerances:
    rowCount: 0
    numericRelative: 0.0001
```

Environment variables and secret stores should resolve protected values at
runtime. Secret values must not be persisted in the manifest or reports.

## Interfaces

### Agent and MCP

The agent should use project-level tools rather than requiring customers to
manually chain low-level tools:

```text
create_migration_project
run_inventory
create_conversion_plan
record_migration_decision
convert_approved_packages
validate_migration_release
deploy_staging_release
run_reconciliation
publish_readiness_report
deploy_approved_release
```

Existing package-level tools can remain available for engineering and debugging.

### CLI

The CLI provides repeatability for engineers and automation:

```powershell
ssis-migrate project create migration.yaml
ssis-migrate inventory migration.yaml
ssis-migrate assess migration.yaml
ssis-migrate convert migration.yaml
ssis-migrate validate migration.yaml
ssis-migrate deploy --environment staging migration.yaml
ssis-migrate reconcile migration.yaml
ssis-migrate report migration.yaml
```

### API and portal

An API enables hosted and enterprise operation. A portal can provide:

- Estate inventory and migration waves
- Package lineage and dependency views
- Findings, decisions, and approvals
- Conversion coverage and readiness dashboards
- Reconciliation results
- Release and deployment history

## Initial product boundary

The first trustworthy release should intentionally support a narrower set of
components:

- File system and Git discovery
- Execute SQL Tasks
- Simple one-source, one-destination Data Flow Tasks
- Execute Package Tasks
- Basic variables and parameters
- Success, failure, and completion precedence constraints
- SQL Server, Azure SQL, and flat-file connectors
- SQL Agent schedule inventory
- Row-count, schema, aggregate, and key reconciliation

Anything outside this boundary should still be inventoried precisely but should
block automatic production readiness.

## Delivery roadmap

### Phase 1: Trustworthy inventory

- Add migration project and source snapshot models
- Create estate-level discovery across packages
- Introduce stable identifiers and versioned IR serialization
- Capture parser coverage and unparsed evidence
- Build dependency and lineage graphs
- Produce JSON and human-readable inventory reports
- Establish a representative, sanitized SSIS fixture corpus

**Exit criteria:** The system can account for every executable source component
and clearly identify anything it could not parse.

### Phase 2: Assessment and planning

- Add the conversion capability registry
- Separate complexity, convertibility, and risk
- Generate package-level and estate-level conversion plans
- Add decisions, assumptions, exceptions, and approval records
- Use AI to explain packages and recommend migration patterns

**Exit criteria:** Every source component has a documented disposition and no
unsupported behavior can be mistaken for converted behavior.

### Phase 3: Narrow deterministic conversion

- Harden the initial supported component set
- Add source-to-target traceability metadata
- Generate a release manifest with rule versions
- Replace TODO-shaped success with blocking findings
- Validate all artifact references and expressions
- Add golden-file and semantic conversion tests

**Exit criteria:** Supported patterns generate repeatable, internally consistent
ADF releases; unsupported patterns block release.

### Phase 4: Staging and reconciliation

- Add staging deployment workflows
- Generate reconciliation specifications
- Capture SSIS and ADF execution metrics
- Compare schemas, counts, keys, aggregates, and watermarks
- Persist evidence and diagnose differences with AI assistance

**Exit criteria:** A migrated package cannot be marked ready without required
reconciliation evidence or an approved exception.

### Phase 5: Enterprise workflow

- Add API and persistent workflow orchestration
- Add multi-user decisions and approvals
- Add migration waves and portfolio dashboards
- Add policy packs and CI/CD integration
- Add audit, access control, observability, and retention controls

**Exit criteria:** Customers can operate repeatable migration programs across
large SSIS estates.

### Phase 6: Expanded conversion intelligence

- Expand supported control-flow and data-flow components
- Add AI-assisted Script Task and SQL modernization
- Add reusable organization-specific mapping knowledge
- Add automated repair loops driven by validation and reconciliation failures
- Benchmark accuracy and effort reduction against the fixture corpus

**Exit criteria:** AI assistance measurably reduces manual effort without weakening
the readiness gates.

## Changes required in this repository

The current repository provides useful foundations but needs these structural
changes:

1. Move orchestration out of `mcp_server.py` into reusable application services.
2. Keep MCP handlers thin so CLI, API, and tests use the same services.
3. Add migration project, release, decision, evidence, and workflow models.
4. Version the canonical IR and conversion rules.
5. Replace structural-only validation with deep artifact and reference validation.
6. Treat complex Mapping Data Flow TODO output as incomplete, not successful.
7. Block dependency cycles rather than silently appending cyclic tasks.
8. Add estate-level processing instead of package-only operations.
9. Add reconciliation generators and execution adapters.
10. Build a broad fixture and regression suite before expanding converter coverage.

## Success measures

Product success should be measured through:

- Percentage of estate fully inventoried
- Parse and conversion coverage by component
- Reconciliation pass rate
- Percentage of migrations ready without unapproved exceptions
- Manual engineering hours saved
- Time from discovery to staging
- Defects found after readiness approval
- AI suggestion acceptance and correction rates
- Repeatability across identical source snapshots

The primary success metric is not the number of ADF files generated. It is the
percentage of migrated workloads that achieve evidence-backed readiness with less
customer effort.

## Near-term decisions

Before implementation begins, confirm:

1. Whether the first release is local-first, hosted, or both
2. Which SSIS and SQL Server versions form the initial support matrix
3. Which ADF connectors and activities are in the initial trusted boundary
4. Where migration project state and evidence will be stored
5. How baseline SSIS executions will be triggered and observed
6. Which reconciliation checks are mandatory by default
7. Which AI provider and data-handling policies are permitted
8. Which actions require explicit customer approval

These decisions should be captured as architecture decision records as the
platform evolves.
