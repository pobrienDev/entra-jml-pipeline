# entra-jml-pipeline

[![ci](https://github.com/pobrienDev/entra-jml-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/pobrienDev/entra-jml-pipeline/actions/workflows/ci.yml)

> **Lab project.** Built and run entirely in a personal lab tenant on an Azure
> free-tier subscription, with made-up employees. It is not connected to any
> production system.

Creating accounts by hand is slow and easy to get wrong. A new hire waits on
day one, someone who changed teams keeps access they no longer need, and
someone who left stays signed in until a person remembers to act. This
project does that work from the HR record instead: an export lands in
storage, and the pipeline creates, updates or offboards the Microsoft Entra
ID accounts to match, with no stored credentials anywhere.

It is a cloud-native redesign of
[employee-provisioning-tool](https://github.com/pobrienDev/employee-provisioning-tool),
my onboarding CLI, which cut onboarding from 20 to 30 minutes to 1 or 2. The
same Microsoft Graph logic is rebuilt as an event-driven service that runs on
a managed identity instead of a client secret, and extended with the mover
step the CLI doesn't have.

## How it works

```mermaid
flowchart LR
    hr[HR CSV export] --> feed[(hr-feed<br/>blob container)]
    feed -- "blob created<br/>(Event Grid)" --> parser[Parser<br/>function]
    parser <--> snap[(snapshot of the<br/>last export)]
    parser -- "one message<br/>per change" --> queue[[jml-events<br/>queue]]
    queue --> processor[Processor<br/>function]
    processor -- "managed identity" --> graph[Microsoft Graph<br/>Entra ID]
    processor --> audit[(audit table)]
    queue -. "after 5 failed<br/>attempts" .-> poison[[poison queue]]
    processor -. logs .-> insights[Application<br/>Insights]
    parser -. logs .-> insights
    insights --> alert{{alert}}
```

1. An HR CSV is uploaded to the `hr-feed` container.
2. Event Grid calls the **parser**. It validates the file, compares it with a
   snapshot of the last export and queues one message per person who changed:
   join, move or leave.
3. The **processor** takes each message and carries it out against Microsoft
   Graph, as a managed identity.
4. Every action is written to an audit table. A message that keeps failing
   lands in a poison queue, which raises an alert.

Every resource is defined in Terraform. Code is deployed by GitHub Actions
through OIDC, with no stored credentials.

| What happens in HR | What the pipeline does |
|---|---|
| **Join:** an employee ID appears | Creates the account with a collision-safe UPN; sets department, title and hire date; adds the department's groups; sets the manager |
| **Move:** department, title or manager changes | Updates the attributes; adds the new role's groups, then removes the old ones; updates the manager |
| **Leave:** someone drops off the feed or is marked terminated | Disables sign-in and revokes sessions first; then removes the managed groups and direct licenses; stamps the leave date |

## Run it twice, nothing changes

The property the whole design is built around: processing the same file
again must change nothing. These are from the lab.

The day-1 file, 20 people, run for the first time and then again:

```
Summary: 84 done, 0 would, 0 skipped, 0 failed
Summary: 0 done, 0 would, 84 skipped, 0 failed
```

The day-2 file uploaded to the feed, then uploaded again:

```
parser: hr_export_day2.csv -> 2 leave, 4 move; 0 manager message(s); 0 row(s) rejected
parser: hr_export_day2.csv -> no changes; 0 manager message(s); 0 row(s) rejected
```

It holds at two levels. The parser only queues what differs from the last
export, so a repeat upload queues nothing. And if a message is delivered
twice anyway, every step in the processor reads the directory first and acts
only on the difference: an account that exists isn't recreated, a membership
that exists isn't re-added.

That second level is what makes failure cheap. During lab testing a newly
granted permission had not taken effect when two leavers were processed.
Both were locked out and removed from their groups, then the last step was
refused. The queue retried; each retry skipped what was already done; the
third attempt finished the job. Nobody intervened.

## Design decisions

**Why a queue between the two functions.** One export can mean hundreds of
changes, and each is several Graph calls that can be throttled or fail
independently. With one message per person, a failure belongs to that person
alone: the queue retries it five times, then sets it aside in a poison queue
where it is visible and nothing is lost, while everyone else's changes go
through. The processor contains no retry loops and swallows no errors. It
raises, and the platform does the rest.

**Why a managed identity.** The CLI this replaces authenticates with a client
secret in a `.env` file: something to store, rotate and possibly leak, with
tenant-wide write access behind it. Here Azure issues the Function app its
tokens at runtime. The storage account has account keys disabled, so no
connection string exists either, and Application Insights accepts telemetry
only from Entra ID-authenticated callers. There is no secret in the code, the
configuration, the Terraform state or the repository.

**Why two identities.** The one the pipeline runs as can create and disable
accounts across the tenant. The one GitHub Actions deploys as can push code
to a single Function app and nothing else: no Graph permissions, no storage
access, no rights over infrastructure. A compromised workflow could ship bad
code, which a required reviewer on the deployment environment gates; it could
not touch the directory directly. Terraform is applied by a person.

**Why idempotency, and why `employeeId`.** A service that runs unattended
will be retried, redelivered and re-run. If a repeat can create a second
account or strip a group twice, every retry is a risk; if it can't, retries
are free and failure handling becomes simple. People are matched on the HR
system's ID, never on name: names change and collide, the ID does not.

**Why diff against a snapshot.** The pipeline has to know who *left*, and a
file can't say who is missing from it. Comparing each export with the last
one turns "no longer listed" into an event. It also means a row rejected for
a typo can be carried forward as last known, so a mistake in one row never
reads as that person resigning.

**Why a mass-change brake.** A truncated or wrong file looks, to a diff,
exactly like most of the company leaving. If one export would offboard more
than 10% of the people the pipeline knows, nothing in it is processed, the
snapshot is left alone, and a person decides. One leaver is always allowed.

**Why only managed groups are ever removed.** The pipeline removes a
membership only if the department map names the group. Anything else on an
account was granted by someone else for a reason the pipeline can't see.
Those memberships, and any directory roles, are recorded as follow-ups for a
person instead.

**Why the manager is a second message.** Messages are processed in parallel
and the feed is in no particular order, so a new hire can be processed before
their manager's account exists. Each joiner's manager is set by a separate
message held back 90 seconds. If the manager still isn't there, that one
message retries and then poisons.

**Why two resources bypass the standard Terraform provider.** The audit table
and the Function app are created through the Azure management API (`azapi`),
because the `azurerm` resources for both assume account keys: one makes a
key-only call on every refresh, the other writes a connection string with an
empty key that overrides the identity settings. Both failures only showed up
by deploying, and both are explained where the resources are defined.

**Why the alert watches logs, not the poison queue.** Azure reports queue
length only for a whole storage account, hourly. The Function app instead
writes one log line when a message fails its last attempt and one when the
brake refuses a file. An alert on those lines names the person and the
reason.

## Guardrails

- **Dry run.** With `dry_run = true` the processor reads the directory,
  works out every step and records it in the audit table as `would`. It
  changes nothing, and the snapshot is not advanced, so the same export
  replays for real once dry run is switched off. The local runner is a dry
  run unless given `--apply`.
- **The audit table.** One row per action: who it was about, what was done,
  to what, the result, the export that caused it and which attempt it was.
  Rejected rows, refused files and poisoned messages are recorded too.
- **Lock out before clean up.** A leaver is disabled and has their sessions
  revoked before anything else, and if the disable fails nothing further is
  attempted. The leave date is stamped last, only when everything before it
  worked.
- **New before old.** A mover gains the new role's groups before losing the
  old ones, so nobody is without access in between.
- **Disabled, never deleted.** Offboarding is reversible.
- **Wrong-tenant checks.** Terraform is pinned to an explicit tenant and
  subscription. The local runner stops unless the domain it is given belongs
  to the tenant the Azure CLI is signed in to.
- **The parser reads only its own feed.** Whatever an event points at, it
  opens nothing but CSVs in the configured container of its own storage
  account.

## Security and permissions

Microsoft Graph application permissions, granted to the pipeline's managed
identity by `scripts/grant-graph-perms.sh`:

| Permission | Why |
|---|---|
| `User.ReadWrite.All` | Create and update users, set manager, disable sign-in |
| `GroupMember.ReadWrite.All` | Add and remove group members. Narrower than `Group.ReadWrite.All`: the pipeline never creates or reconfigures a group |
| `User.RevokeSessions.All` | Revoke sign-in sessions when offboarding |
| `User-LifeCycleInfo.ReadWrite.All` | Stamp the leave date on an offboarded account. It covers that one attribute, which `User.ReadWrite.All` cannot write |

The Azure portal can't grant Graph permissions to a managed identity, so the
script assigns them through the Graph API. It checks before it grants, and
`--check` reports without changing anything. `Group.Read.All` turned out not
to be needed: finding the managed groups by name works with the permissions
above.

Azure roles:

| Identity | Role | Scope |
|---|---|---|
| Pipeline | Storage Blob Data Owner | The storage account. Owner, not Contributor, because the Functions host requires it for its own storage when using an identity |
| Pipeline | Storage Queue Data Contributor, Storage Table Data Contributor | The storage account |
| Pipeline | Monitoring Metrics Publisher | Application Insights |
| Deploy (GitHub Actions) | Website Contributor | The one Function app |
| Operator (you) | Storage Blob Data Contributor | The `hr-feed` container only |
| Operator (you) | Storage Table Data Reader | The audit table only |

Nothing tenant-specific is committed. Tenant and subscription IDs live in a
gitignored `terraform.tfvars`, the state storage account in a gitignored
backend file, and the tenant's domain is looked up at apply time. A
pre-commit hook blocks IDs, tenant domains, state files and secret-shaped
strings.

## What came from employee-provisioning-tool

| | What | Notes |
|---|---|---|
| Copied unchanged | Request handling, retries, paging and error handling; user, group and license calls; UPN generation and its transliteration table; license and group-type helpers; their tests | Taken line for line from the original |
| Copied, then changed | Authentication | A client secret from `.env` became a credential object: the managed identity in Azure, the Azure CLI sign-in locally |
| Copied, then changed | Offboarding | The CLI's `terminate` command, without its prompts and printing, as a function a queue can call. Same order: lock out, then clean up. It removes only managed groups, where the CLI removes all it can |
| Rewritten | Group selection and display names | The CLI's rules follow that tool's own business model. Here they follow a department map |
| Left behind | Mailbox features, the delegated sign-in, MFA reset, licensing rules, the command-line interface | They need a signed-in person, or the lab tenant has nothing for them to act on |
| New | `employeeId` matching, manager assignment, HR feed parsing, the idempotent joiner, the mover, snapshot diffing, the audit table, the mass-change brake, dry run, all the infrastructure | |

The production repository was only ever read. Nothing was changed there, and
no configuration, credentials or logs were copied from it.

## Repository layout

```
.
├── infra/                       Terraform for every Azure and Entra ID resource
│   ├── main.tf                  resource group, the pipeline's managed identity
│   ├── storage.tf               storage account, containers, queues, audit table, roles
│   ├── function.tf              Flex Consumption plan, Function app and its settings
│   ├── eventgrid.tf             delivers "CSV uploaded" events to the parser
│   ├── groups.tf                the Entra ID groups named in the department map
│   ├── monitoring.tf            Log Analytics workspace, Application Insights
│   ├── alerts.tf                alert when a message is poisoned or an export is refused
│   ├── budget.tf                monthly cost alert
│   ├── github.tf                the identity GitHub Actions deploys as
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
│       ├── events.py            export to events, carrying out one event, the brake
│       ├── onboard.py           the joiner
│       ├── mover.py             the mover
│       ├── offboard.py          the leaver
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
│   ├── deploy.sh                builds and deploys it
│   ├── run_local.py             runs the joiner from a developer machine
│   └── teardown.sh              removes the accounts and the infrastructure
├── tests/                       Microsoft Graph and storage faked throughout, no network
├── docs/demo.md                 a five-minute walkthrough
├── .github/workflows/           ci.yml (lint and test), deploy.yml (OIDC)
└── .githooks/pre-commit         blocks IDs, secrets and state from being committed
```

## Testing

More than 200 tests, none of which touch a network. Microsoft Graph is
replaced by an in-memory directory that answers the reads and records every
write, so a test can assert exactly what would have changed.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

- **Idempotency:** both sample files processed end to end, then every day-2
  message delivered a second time. The second delivery makes no writes.
- **Failure:** a malformed row and a nonexistent manager. The row is rejected
  and its person left alone; the manager step fails, retries and is poisoned;
  every other row proceeds.
- **Brake:** a file that drops half the company is halted. Nothing is queued
  and the snapshot is untouched.
- **Unit:** CSV validation, diffing, group selection, UPN collisions, and the
  Graph client's retry, paging and error handling.

The idempotency, failure and brake scenarios were then run for real in the
lab, along with dry run and the alert.

## Running it in your own lab tenant

Requires Terraform 1.9 or later, the Azure CLI, Python 3.12 or later, and an
Azure Storage container for remote state.

```bash
az login --tenant <your-lab-tenant>

cd infra
cp terraform.tfvars.example terraform.tfvars        # tenant and subscription IDs
cp backend.tfbackend.example backend.tfbackend      # the state storage account
terraform init -backend-config=backend.tfbackend
terraform apply -var event_subscription_enabled=false
cd ..

scripts/grant-graph-perms.sh
scripts/deploy.sh

terraform -chdir=infra apply
```

The first apply holds back the Event Grid subscription because Event Grid
checks that the parser function exists before it will deliver to it. Once the
code is deployed, the second apply adds it.

Neither copied file is tracked by git. Turn on the leak check once per clone:

```bash
git config core.hooksPath .githooks
```

Then drop an export in the feed:

```bash
az storage blob upload --auth-mode login \
  --account-name "$(terraform -chdir=infra output -raw storage_account_name)" \
  --container-name hr-feed --file sample-data/hr_export_day1.csv --overwrite
```

Within a minute or two the accounts exist. Upload it again and nothing
happens. Upload `hr_export_day2.csv` for the moves and leavers.
[docs/demo.md](docs/demo.md) walks through it.

### Deploying from GitHub Actions

`deploy.yml` deploys the code on every merge to `main` that touches it. To
set it up on a fork:

1. Set `github_subject_prefix` to your repository's own value and apply:
   `gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix`
2. Create a GitHub environment named `lab`. Give it a required reviewer and
   restrict it to the `main` branch.
3. Add five secrets to that environment. None is a credential; they are
   secrets so that GitHub masks them in public logs.

   | Secret | Value |
   |---|---|
   | `AZURE_CLIENT_ID` | `terraform -chdir=infra output -raw deploy_identity_client_id` |
   | `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `az account show --query "{tenant:tenantId, sub:id}"` |
   | `FUNCTION_APP_NAME` | `terraform -chdir=infra output -raw function_app_name` |
   | `RESOURCE_GROUP` | `terraform -chdir=infra output -raw resource_group_name` |

### Running the joiner locally

The same code the Function app runs, with your Azure CLI sign-in as the
credential:

```bash
# Dry run: reads the directory, changes nothing
.venv/bin/python scripts/run_local.py sample-data/hr_export_day1.csv --domain <your-lab-domain>

# For real
.venv/bin/python scripts/run_local.py sample-data/hr_export_day1.csv --domain <your-lab-domain> --apply
```

## Cost and teardown

At lab volumes this costs close to nothing: Flex Consumption scales to zero,
and the queues, table and logs see a few kilobytes. A budget on the resource
group emails subscription owners at 80% of $5 a month, and log ingestion is
capped at 1 GB a day.

```bash
scripts/teardown.sh --list     # show the accounts that would be deleted
scripts/teardown.sh            # delete them, then destroy the infrastructure
```

The accounts need their own step because the pipeline created them, not
Terraform. The script finds them by employee ID, lists them, and deletes them
only after you type the tenant's name. `terraform destroy` then removes the
resource group, the groups, both identities and the Graph permissions.

## Limits

- **A returning leaver is not re-enabled.** If someone offboarded reappears
  in a later export, their disabled account is left as it is. Rehire is a
  decision for a person.
- **New accounts get a random temporary password that nobody is shown.** It
  is never logged or stored. An account needs a password reset, or a
  Temporary Access Pass, before anyone can sign in to it.
- **The alert takes up to 15 minutes**, which is how often it is evaluated.
- **Licensing is not exercised.** The leaver removes directly assigned
  licenses and the code is tested, but the lab tenant has none to remove.
- **Name changes are not acted on.** A changed name in HR does not create a
  second account, but it does not rename the existing one either.

Not built: Temporary Access Pass for new hires, scheduled hard-delete of
offboarded accounts after 30 days, manager notifications, license assignment
by department, and a report page over the audit table.
