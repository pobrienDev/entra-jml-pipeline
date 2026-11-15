# Demo walkthrough

About five minutes. It shows the three things the project is about: an upload
becomes accounts with no manual step, a repeat upload changes nothing, and a
bad file is stopped.

It assumes a deployed environment (see the README) and an Azure CLI session
in the lab tenant. Have the Entra admin center open on **Users**, filtered to
the lab's test accounts, in a window beside the terminal.

Set this once:

```bash
ACCOUNT="$(terraform -chdir=infra output -raw storage_account_name)"
upload() { az storage blob upload --auth-mode login --account-name "$ACCOUNT" \
  --container-name hr-feed --file "$1" --overwrite --output none && echo "uploaded $1"; }
```

## 1. Day one: an upload becomes accounts

Show the file, then upload it.

```bash
column -s, -t sample-data/hr_export_day1.csv | head -8
upload sample-data/hr_export_day1.csv
```

Refresh the Users list after about a minute. Twenty accounts appear, each
with a department, title, manager and groups. Point out two things:

- `Søren Østergaard` became `sostergaard@`, and `Lukas Strauß` became
  `lstrauss@`: accents are reduced for the address, not for the name.
- `Jordan Lee` is `jlee@` and `Jamie Lee` is `jalee@`: the second took the
  next free address.

## 2. Again: nothing changes

```bash
upload sample-data/hr_export_day1.csv
```

Nothing happens in the Users list. Show why, in Application Insights
(**Logs**):

```kusto
traces
| where message startswith "parser:"
| project timestamp, message
| order by timestamp desc
| take 5
```

The second upload reads `no changes`.

## 3. Day two: movers and leavers

```bash
diff <(sort sample-data/hr_export_day1.csv) <(sort sample-data/hr_export_day2.csv)
upload sample-data/hr_export_day2.csv
```

After a minute, in the Users list:

- `Jordan Lee` moved from Engineering to IT. Open the account: the
  department, title and manager changed, and the group membership swapped.
- `Lukas Strauß` was promoted to Finance Manager and gained
  `jml-people-managers`.
- `Fatima Haddad` and `Owen Gallagher` are **disabled**, with no groups. They
  are not deleted.
- `Ravi Kapoor` and `Jesse Lee` are new.

## 4. The audit trail

Every action, with its result and the export that caused it:

```bash
az storage entity query --auth-mode login --account-name "$ACCOUNT" --table-name jmlaudit \
  --filter "PartitionKey eq '$(date -u +%Y-%m-%d)'" \
  --query "items[].[employee_id, event_type, action, result, target]" --output table
```

## 5. A bad file is stopped

Cut the day-two file in half, as if the export had been truncated, and upload
it.

```bash
head -11 sample-data/hr_export_day2.csv > /tmp/truncated.csv
upload /tmp/truncated.csv
```

Nobody is offboarded. In Application Insights:

```kusto
traces
| where message startswith "brake:"
| project timestamp, message
```

The line reads that ten people would be offboarded and the limit is 10%. The
snapshot was left alone, so uploading the correct file again simply works:

```bash
upload sample-data/hr_export_day2.csv     # no changes
```

Within 15 minutes the **JML pipeline needs attention** alert shows under
**Monitor > Alerts**.

## Resetting

To run the demo again from nothing, remove the accounts and rebuild:

```bash
scripts/teardown.sh
```

Then follow the README's deployment steps.

## For a short recording

Steps 1 and 2 are the whole story in under a minute: upload, accounts appear,
upload again, nothing changes. Record the terminal and the Users list side by
side.
