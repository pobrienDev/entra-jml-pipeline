#!/usr/bin/env bash
# Removes everything this project put in the lab tenant.
#
# Two parts, because two different things created them:
#
#   1. The accounts. The pipeline created these, not Terraform, so
#      `terraform destroy` doesn't know they exist. They are found by their
#      employeeId prefix, listed, and deleted only after you confirm.
#   2. The infrastructure. `terraform destroy` removes the resource group and
#      everything in it, the groups, both managed identities and, with them,
#      the pipeline's Microsoft Graph permissions.
#
# Usage:
#   scripts/teardown.sh --list         show the accounts that would be deleted; change nothing
#   scripts/teardown.sh                delete the accounts, then destroy the infrastructure
#   scripts/teardown.sh --users-only   delete the accounts and keep the infrastructure
#
# Options:
#   --prefix E1    employeeId prefix that marks an account as the pipeline's
#                  (default E1, which matches the sample data)
#
# Deleted accounts go to Entra ID's recycle bin for 30 days.
#
# Requires: Azure CLI signed in to the LAB tenant as an admin; Terraform
# initialised in infra/.

set -euo pipefail

GRAPH="https://graph.microsoft.com/v1.0"
prefix="E1"
mode="all"

while [ $# -gt 0 ]; do
  case "$1" in
    --list) mode="list" ;;
    --users-only) mode="users" ;;
    --prefix)
      prefix="${2:?--prefix needs a value}"
      shift
      ;;
    -h | --help)
      sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

# The prefix goes into a Graph filter. Letters and digits only.
if ! [[ "$prefix" =~ ^[A-Za-z0-9]+$ ]]; then
  echo "--prefix must be letters and digits only." >&2
  exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

# Say which tenant this is about to change, by name, before changing it.
tenant_name="$(az rest --method GET --url "$GRAPH/organization?\$select=displayName" --query "value[0].displayName" -o tsv)"
echo "Tenant: $tenant_name"
echo

# Every account whose employeeId starts with the prefix (up to 999, far more
# than a lab holds).
accounts="$(az rest --method GET \
  --url "$GRAPH/users?\$filter=startswith(employeeId,'$prefix')&\$select=id,userPrincipalName,employeeId,accountEnabled&\$top=999" \
  --query "value[].[id, employeeId, userPrincipalName, to_string(accountEnabled)]" -o tsv)"
count="$(grep -c . <<<"$accounts" || true)"

echo "Accounts with an employeeId starting \"$prefix\": $count"
if [ "$count" -gt 0 ]; then
  while IFS=$'\t' read -r _ employee_id upn enabled; do
    printf '  %-8s %-45s %s\n' "$employee_id" "$upn" "$([ "$enabled" = "true" ] && echo enabled || echo disabled)"
  done <<<"$accounts" | sort
fi
echo

if [ "$mode" = "list" ]; then
  echo "Nothing changed. Run without --list to delete these accounts."
  exit 0
fi

if [ "$count" -gt 0 ]; then
  echo "This deletes the $count account(s) above from \"$tenant_name\"."
  read -r -p "Type the tenant's name to confirm: " reply
  if [ "$reply" != "$tenant_name" ]; then
    echo "Not confirmed. Nothing changed."
    exit 1
  fi
  while IFS=$'\t' read -r id employee_id upn _; do
    az rest --method DELETE --url "$GRAPH/users/$id" --output none
    echo "  deleted $employee_id $upn"
  done <<<"$accounts"
  echo
fi

if [ "$mode" = "users" ]; then
  echo "Accounts removed. Infrastructure left in place."
  exit 0
fi

# Terraform asks for its own confirmation and shows what it will destroy.
echo "Destroying the infrastructure..."
terraform -chdir="$repo_root/infra" destroy
