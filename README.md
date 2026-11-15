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
| 1 | Foundation: infrastructure as code, managed identity, Graph permissions | done |
| 2 | Bring over the Graph client and decision logic; joiner runs locally | done |
| 3 | Parser and processor functions wired up in Azure | done |
| 4 | Mover, leaver, idempotency, audit log, safety brakes | in review |
| 5 | CI/CD with OIDC, full documentation, demo | not started |

## How it works

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
│   ├── alerts.tf                alert when a message is poisoned or an export is refused
│   ├── groups.tf                the Entra ID groups named in the department map
│   ├── eventgrid.tf             delivers "CSV uploaded" events to the parser
│   └── backend.tf               remote state (partial config, no names committed)
├── src/
│   ├── function_app.py          the two functions: parser and processor
│   ├── host.json                queue retry settings
│   └── shared/
│       ├── graph_api.py         every Microsoft Graph call, with retries and paging
│       ├── auth.py              where the token comes from: managed identity or Azure CLI
│       ├── rules.py             UPN generation, groups from the department map
│       ├── hr_feed.py           reads and validates the HR CSV, row by row
│       ├── diff.py              compares an export with the last one: join, move, leave
│       ├── events.py            export to events, and carrying out one event
│       ├── onboard.py           the joiner: account, groups, manager
│       ├── mover.py             the mover: attributes, group swap, manager
│       ├── offboard.py          the leaver: lock out, clean up, stamp the date
│       ├── audit.py             writes every action to the audit table
│       ├── storage.py           the feed, the snapshot and the queue
│       └── config.py            loads the department map
├── config/
│   └── department_groups.json   which groups each department and title gets
├── sample-data/
│   ├── hr_export_day1.csv       20 made-up employees across 5 departments
│   └── hr_export_day2.csv       the next day: 2 joiners, 4 movers, 2 leavers
├── scripts/
│   ├── grant-graph-perms.sh     grants the managed identity its Graph permissions
│   ├── build_package.sh         builds the Function app zip
│   ├── deploy.sh                builds and deploys it to the lab
│   └── run_local.py             runs the joiner from a developer machine
├── tests/                       Microsoft Graph faked throughout, no network
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
| `User-LifeCycleInfo.ReadWrite.All` | Stamp the leave date on an offboarded account. It covers that one attribute, which `User.ReadWrite.All` cannot write |

The Azure portal can't grant Graph permissions to a managed identity, so the
script assigns them through the Graph API. It checks before it grants, so
running it twice changes nothing, and `--check` reports without changing
anything.

### The joiner

For each active employee in the HR export, the joiner makes sure they have an
account, are in their groups and report to their manager:

- **People are matched on `employeeId`, never on name.** A name change in HR
  doesn't create a second account, and two people with the same name don't
  collide.
- **Every step checks before it acts.** An account that exists isn't
  recreated; a membership that exists isn't re-added; a manager that is
  already set isn't set again. Running the same file twice changes nothing
  the second time, and a run that died halfway is finished by running it
  again.
- **UPNs are collision-safe.** First initial plus surname, then more letters
  of the first name, then numbers; accents are reduced (`Lefèvre` becomes
  `lefevre`, `Strauß` becomes `strauss`); and an address already in use as
  someone's mail alias counts as taken.
- **It only touches groups named in the department map.** Any other
  membership was granted by someone else and is left alone.
- **One bad row doesn't stop the file.** A row that fails validation is
  reported and skipped; a step that fails is recorded against that person
  and the rest continue.
- **Dry run by default.** The local runner reads the directory and reports
  what it would do. Nothing changes without `--apply`.

### From upload to accounts

- **The parser diffs, it doesn't replay.** It keeps a snapshot of the last
  export and queues one message per person who changed. Uploading the same
  file again queues nothing.
- **A typo is not a resignation.** A row that fails validation is reported,
  and that person is carried forward as last known instead of looking like
  they left.
- **Failure is the queue's job.** If the processor can't finish an event it
  raises, and the queue retries the message five times, at least 30 seconds apart,
  then moves it to the poison queue. Nothing is lost and nothing is caught
  and ignored. Because every step checks before it acts, a retry only does
  what is left.
- **Managers wait their turn.** A manager is set by a second message, held
  back 90 seconds so both accounts exist. If the manager still isn't there,
  that message retries and then poisons, without affecting anyone else.
- **Two uploads at once can't corrupt the snapshot.** It is written only if
  it hasn't changed since it was read; the loser's trigger runs again
  against the newer one.
- **The parser reads only its own feed.** Whatever an event points at, it
  opens nothing but CSVs in the configured container of its own storage
  account.

### The mover

When someone's department, title or manager changes:

- **Attributes follow the HR record.** The account is compared with the
  record itself, not with what the event says changed, so a half-applied
  move is finished by running it again.
- **Groups are swapped, new before old.** The groups the new role needs are
  added before the ones it doesn't are removed, so nobody is left without
  access in between.
- **Only managed groups are ever removed.** A membership in a group the
  department map doesn't name was granted by someone else, and stays.
- **The manager follows too**, including being cleared when HR lists none.

### The leaver

When someone leaves the feed or is marked terminated:

1. **Lock out first.** The account is disabled and its sessions revoked
   before any clean-up, so a failure later can't leave someone signed in.
2. **Leave the managed groups.** Other memberships and any directory roles
   are not touched; they are recorded as follow-ups for a person.
3. **Free directly assigned licenses.**
4. **Stamp the leave date** on the account, last, and only if everything
   before it worked. That stamp is what a later clean-up job would read, and
   what tells a repeat run the account is already finished.

The account is disabled, never deleted.

### Guardrails

- **The mass-change brake.** A truncated file looks, to a diff, exactly like
  most of the company resigning. If one export would offboard more than 10%
  of the people the pipeline knows, nothing in it is processed, the snapshot
  is left alone and an alert fires. Fix the file and upload it again, or
  raise `max_leave_percent` on purpose.
- **Dry run.** With `dry_run = true` the processor reads the directory,
  works out every step and records it in the audit table as "would", and
  changes nothing.
- **The audit table.** One row per action: who it was about, what was done,
  to what, the result, the export that caused it and which attempt it was.
  Partitioned by date.
- **An alert a person will see.** Fires when a message has failed its fifth
  and last attempt, or when the brake refuses an export.

### What came from employee-provisioning-tool

| | What | Notes |
|---|---|---|
| Copied unchanged | Request handling, retries, paging and error handling; user, group and license calls; UPN generation and its transliteration table; their tests | Taken line for line from the original |
| Copied, then changed | Authentication | A client secret from `.env` became a credential object: the managed identity in Azure, the Azure CLI sign-in locally |
| Copied, then changed | Offboarding | The CLI's `terminate` command, without its prompts and printing, as a function a queue can call. Same order: lock out, then clean up. It removes only managed groups, where the CLI removes all it can |
| Rewritten | Group selection and display names | The CLI's rules follow that tool's own business model. Here they follow a department map |
| Left behind | Mailbox features, the delegated sign-in, MFA reset, licensing, the command-line interface | They need a signed-in person, or the lab tenant has nothing for them to act on |
| New | `employeeId` matching, manager assignment, HR feed parsing, the idempotent joiner, the mover, snapshot diffing, the audit table, the mass-change brake, the dry run | |

## Running the joiner locally

Requires Python 3.10 or later and an Azure CLI session in the lab tenant.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q

# Dry run: reads the directory, changes nothing
.venv/bin/python scripts/run_local.py sample-data/hr_export_day1.csv --domain <your-lab-domain>

# For real
.venv/bin/python scripts/run_local.py sample-data/hr_export_day1.csv --domain <your-lab-domain> --apply
```

`--domain` is the UPN domain new accounts get. The run stops unless it is a
verified domain of the tenant the Azure CLI is signed in to, so a session
left signed in to a different tenant can't be changed by accident.

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
scripts/deploy.sh
```

Event Grid checks that the parser function exists before it will deliver to
it. On a brand-new environment, run the first apply with
`-var event_subscription_enabled=false`, deploy the code, then apply again.

Then drop an export in the feed and watch the accounts appear:

```bash
az storage blob upload --auth-mode login \
  --account-name "$(terraform -chdir=infra output -raw storage_account_name)" \
  --container-name hr-feed --file sample-data/hr_export_day1.csv --overwrite
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
