locals {
  tags = {
    project     = "entra-jml-pipeline"
    managed_by  = "terraform"
    environment = "lab"
  }
}

# Everything this project creates lives in this one resource group, so
# `terraform destroy` (or deleting the group) removes all of it and nothing
# belonging to the other projects in the tenant.
resource "azurerm_resource_group" "main" {
  name     = "rg-${var.name_prefix}-pipeline"
  location = var.location
  tags     = local.tags
}

# Storage account and Function app names are global DNS names, so they need a
# suffix to be unique.
resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

# The one identity the pipeline runs as. User-assigned rather than
# system-assigned so it exists before the Function app does: its storage roles
# can be in place when the app first starts, and its Graph permissions survive
# the app being rebuilt.
#
# It has no secret. Azure issues its tokens to the Function app at runtime, so
# there is nothing to store, rotate or leak.
#
# Its Microsoft Graph permissions are NOT granted here. See
# scripts/grant-graph-perms.sh.
resource "azurerm_user_assigned_identity" "pipeline" {
  name                = "id-${var.name_prefix}-pipeline"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags
}
