---
mode: agent
description: Enforce the build gate and deploy an approved package to Azure Data Factory.
---

# Deploy ADF Artifacts

Validate and deploy generated ADF JSON artifacts to an Azure Data Factory instance.

## Parameters

- **Workspace**: ${input:workspace:Migration workspace, e.g. work/<migration>}
- **Package key**: ${input:package_key:Package key from migration.json}
- **Subscription ID**: ${input:subscription_id:Azure subscription GUID}
- **Resource group**: ${input:resource_group:Azure resource group name}
- **Factory name**: ${input:factory_name:Azure Data Factory name}
- **Dry run?**: ${input:dry_run:true to validate only (no Azure API calls), false to deploy}

## Steps

### 1 — Validate artifacts

Run `ssis-adf validate --workspace "<workspace>" --package "<package_key>"`.

If G2 is blocked by validation or open review items, stop and report them. The CLI also
enforces this gate and will refuse deployment.

### 2 — Dry run (if requested)

If `dry_run` = true, run `ssis-adf deploy` with `--dry-run`.
Report what would be deployed (type, name, count).
**Do not proceed to actual deployment.**

### 3 — Deploy

Run:

`ssis-adf deploy --workspace "<workspace>" --package "<package_key>" --subscription-id "<subscription_id>" --resource-group "<resource_group>" --factory-name "<factory_name>"`

Deployment order enforced by the tool: linked services → datasets → data flows → pipelines → triggers.

### 4 — Deployment report

Produce a Markdown summary:

| Artifact Type | Count | Status |
|---|---|---|
| Linked Services | N | ✅ / ❌ |
| Datasets | N | ✅ / ❌ |
| Data Flows | N | ✅ / ❌ |
| Pipelines | N | ✅ / ❌ |
| Triggers | N | ✅ (Stopped) |

List any failures with their error messages.

### 5 — Post-deployment checklist

Remind the user:
- [ ] **Triggers are deployed in Stopped state** — go to ADF Studio → Manage → Triggers to activate them.
- [ ] Validate linked service connections via "Test Connection" in ADF Studio.
- [ ] Run each pipeline once with debug mode before activating schedules.
- [ ] For pipelines that call Azure Functions: verify Function App URLs and keys in the linked services.
- [ ] Confirm that `az login` or service principal credentials are correctly set if deployment succeeded but subsequent runs fail.
