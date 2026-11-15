#!/usr/bin/env bash
# Grants the pipeline's managed identity its Microsoft Graph application
# permissions.
#
# Why a script: the Azure portal has no screen for giving Graph permissions to
# a managed identity. They can only be assigned through the Graph API, as "app
# role assignments" on the identity's service principal. This is the managed
# identity equivalent of clicking "Grant admin consent" on an app registration.
#
# Usage:
#   scripts/grant-graph-perms.sh            grant anything missing
#   scripts/grant-graph-perms.sh --check    report only, change nothing
#   scripts/grant-graph-perms.sh [--check] <principal-object-id>
#
# With no ID given, it reads managed_identity_principal_id from the Terraform
# outputs in infra/.
#
# Safe to re-run: each permission is checked first and only granted if absent.
#
# Requires: Azure CLI, signed in to the LAB tenant as an admin who can consent
# to Graph application permissions (Privileged Role Administrator or Global
# Administrator).

set -euo pipefail

# Least privilege: exactly what the pipeline's Graph calls need.
PERMISSIONS=(
  "User.ReadWrite.All"        # create and update users, set manager, disable sign-in
  "GroupMember.ReadWrite.All" # add and remove group members; narrower than Group.ReadWrite.All
  "User.RevokeSessions.All"   # revoke sign-in sessions when offboarding
)
# Deliberately not granted: Group.Read.All. Add it only if group lookups
# return 403 without it.

# Microsoft Graph's application ID. Public and identical in every tenant.
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"
GRAPH="https://graph.microsoft.com/v1.0"

check_only=false
principal_id=""
for arg in "$@"; do
  case "$arg" in
    --check) check_only=true ;;
    -h | --help)
      sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
      exit 0
      ;;
    -*)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
    *) principal_id="$arg" ;;
  esac
done

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "$principal_id" ]; then
  principal_id="$(terraform -chdir="$repo_root/infra" output -raw managed_identity_principal_id 2>/dev/null || true)"
fi
if ! [[ "$principal_id" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]]; then
  echo "No managed identity found. Run 'terraform apply' in infra/ first, or pass the identity's principal (object) ID." >&2
  exit 1
fi

# Say which tenant this is about to change, by name, before changing it.
tenant_name="$(az rest --method GET --url "$GRAPH/organization?\$select=displayName" --query "value[0].displayName" -o tsv)"
identity_name="$(az rest --method GET --url "$GRAPH/servicePrincipals/$principal_id?\$select=displayName" --query displayName -o tsv)"
echo "Tenant:   $tenant_name"
echo "Identity: $identity_name"
echo

# Microsoft Graph's service principal in this tenant. Permissions are app roles
# defined on it; the role IDs are looked up by name rather than hardcoded.
graph_sp_id="$(az rest --method GET --url "$GRAPH/servicePrincipals(appId='$GRAPH_APP_ID')?\$select=id" --query id -o tsv)"

# Role IDs the identity already holds on Microsoft Graph.
existing="$(az rest --method GET \
  --url "$GRAPH/servicePrincipals/$principal_id/appRoleAssignments?\$top=999" \
  --query "value[?resourceId=='$graph_sp_id'].appRoleId" -o tsv)"

missing=0
for permission in "${PERMISSIONS[@]}"; do
  role_id="$(az rest --method GET \
    --url "$GRAPH/servicePrincipals/$graph_sp_id?\$select=appRoles" \
    --query "appRoles[?value=='$permission' && contains(allowedMemberTypes, 'Application')].id | [0]" -o tsv)"
  if [ -z "$role_id" ]; then
    echo "  $permission: not a Microsoft Graph application permission" >&2
    exit 1
  fi

  if grep -qix "$role_id" <<<"$existing"; then
    echo "  $permission: already granted"
  elif $check_only; then
    echo "  $permission: MISSING"
    missing=$((missing + 1))
  else
    az rest --method POST \
      --url "$GRAPH/servicePrincipals/$principal_id/appRoleAssignments" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\":\"$principal_id\",\"resourceId\":\"$graph_sp_id\",\"appRoleId\":\"$role_id\"}" \
      --output none
    echo "  $permission: granted"
  fi
done

if $check_only && [ "$missing" -gt 0 ]; then
  echo
  echo "$missing permission(s) missing. Re-run without --check to grant."
  exit 1
fi
