#!/usr/bin/env bash
# Builds the Function app package and deploys it to the lab.
#
# Azure installs the Python dependencies during deployment (--build-remote),
# so nothing is built on this machine. Uses whatever the Azure CLI is signed
# in as: you on a laptop, the deploy identity in GitHub Actions.
#
# The app's name and resource group come from FUNCTION_APP_NAME and
# RESOURCE_GROUP if set (the deploy workflow sets them), otherwise from the
# Terraform outputs in infra/.
#
# Usage: scripts/deploy.sh

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
app="${FUNCTION_APP_NAME:-$(terraform -chdir="$root/infra" output -raw function_app_name)}"
group="${RESOURCE_GROUP:-$(terraform -chdir="$root/infra" output -raw resource_group_name)}"

package="$("$root/scripts/build_package.sh")"
echo "Deploying $(basename "$package") to $app in $group"
az functionapp deployment source config-zip \
  --resource-group "$group" --name "$app" \
  --src "$package" --build-remote true --output none
echo "Deployed. Functions:"
az functionapp function list --resource-group "$group" --name "$app" --query "[].name" --output tsv
