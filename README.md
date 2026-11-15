# entra-jml-pipeline

> **Lab project.** Built and run entirely in a personal lab tenant on an Azure
> free-tier subscription, with made-up employees. It is not connected to any
> production system.

An event-driven joiner / mover / leaver pipeline for Microsoft Entra ID. An HR
export lands in blob storage; the pipeline works out who joined, who changed
role and who left, and creates, updates or offboards their accounts through
Microsoft Graph, with no stored credentials anywhere.

It is a cloud-native redesign of
[employee-provisioning-tool](https://github.com/pobrienDev/employee-provisioning-tool),
my onboarding CLI: the same Graph logic, rebuilt as a serverless service that
runs on a managed identity instead of a client secret, and extended with the
mover step the CLI doesn't have.

## Status

Work in progress, built in five stages.

| Stage | Focus | State |
|---|---|---|
| 1 | Foundation: infrastructure as code, managed identity, Graph permissions | in review |
| 2 | Bring over the Graph client and decision logic; joiner runs locally | not started |
| 3 | Parser and processor functions wired up in Azure | not started |
| 4 | Mover, leaver, idempotency, audit log, safety brakes | not started |
| 5 | CI/CD with OIDC, full documentation, demo | not started |

## How it will work

```
HR CSV ──> Blob container ──(Event Grid)──> Parser function
                                                │ one message per change
                                                ▼
                                          Storage queue ──> Processor function ──> Microsoft Graph
                                                │                  │
                                          Poison queue        Audit table
```

1. An HR CSV is uploaded to the `hr-feed` container.
2. The parser validates it, compares it with the previous snapshot and queues
   one message per change: join, move or leave.
3. The processor handles each message by calling Microsoft Graph as a managed
   identity.
4. Every action is written to an audit table. Messages that keep failing land
   in a poison queue, which raises an alert.

## What exists so far

```
.
├── infra/                       Terraform for every Azure resource
│   ├── main.tf                  resource group, managed identity
│   ├── storage.tf               storage account, HR feed container, queues, audit table, identity roles
│   ├── function.tf              Flex Consumption plan and Function app
│   ├── monitoring.tf            Log Analytics workspace, Application Insights
│   ├── budget.tf                monthly cost alert
│   └── backend.tf               remote state (partial config, no names committed)
├── scripts/
│   └── grant-graph-perms.sh     grants the managed identity its Graph permissions
└── .githooks/pre-commit         blocks IDs, secrets and state from being committed
```

### No secrets

- **One user-assigned managed identity** runs the pipeline. Azure issues its
  tokens at runtime; there is no client secret to store, rotate or leak.
- **The storage account has account keys disabled.** No connection strings
  exist. The identity reaches blobs, queues and tables through role
  assignments only, and the Function app's own host storage uses the same
  identity.
- **Application Insights accepts telemetry only from Entra ID-authenticated
  callers**, so its connection string is not a credential.

### Graph permissions

Application permissions, granted to the managed identity by
`scripts/grant-graph-perms.sh`:

| Permission | Why |
|---|---|
| `User.ReadWrite.All` | Create and update users, set manager, disable sign-in |
| `GroupMember.ReadWrite.All` | Add and remove group members. Narrower than `Group.ReadWrite.All`: the pipeline never creates or reconfigures a group |
| `User.RevokeSessions.All` | Revoke sign-in sessions when offboarding |

The Azure portal can't grant Graph permissions to a managed identity, so the
script assigns them through the Graph API. It checks before it grants, so
running it twice changes nothing, and `--check` reports without changing
anything.

## Deploying to your own lab tenant

Requires Terraform 1.9 or later, the Azure CLI, and an Azure Storage container
for remote state.

```bash
az login --tenant <your-lab-tenant>

cd infra
cp terraform.tfvars.example terraform.tfvars        # fill in tenant and subscription IDs
cp backend.tfbackend.example backend.tfbackend      # fill in the state storage account
terraform init -backend-config=backend.tfbackend
terraform apply

cd ..
scripts/grant-graph-perms.sh
```

Neither copied file is tracked by git. Before committing anything, turn on the
leak check once per clone:

```bash
git config core.hooksPath .githooks
```

## Cost and teardown

At lab volumes the resources here cost close to nothing. A budget on the
resource group emails subscription owners at 80% of a $5 monthly budget.

Everything lives in one resource group, so this removes all of it, including
the managed identity and its Graph permissions:

```bash
cd infra && terraform destroy
```
