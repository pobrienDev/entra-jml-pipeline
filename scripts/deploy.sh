#!/usr/bin/env bash
# Builds the Function app package and deploys it to the lab.
#
# Azure installs the Python dependencies during deployment (--build-remote),
# so nothing is built on this machine. Uses your Azure CLI sign-in; reads the
# app's name from the Terraform outputs in infra/.
#
# Usage: scripts/deploy.sh

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
app="$(terraform -chdir="$root/infra" output -raw function_app_name)"
group="$(terraform -chdir="$root/infra" output -raw resource_group_name)"

package="$("$root/scripts/build_package.sh")"
echo "Deploying $(basename "$package") to $app in $group"
az functionapp deployment source config-zip \
  --resource-group "$group" --name "$app" \
  --src "$package" --build-remote true --output none
echo "Deployed. Functions:"
az functionapp function list --resource-group "$group" --name "$app" --query "[].name" --output tsv
