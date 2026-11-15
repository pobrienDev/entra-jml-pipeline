# Pinned to an explicit tenant and subscription rather than inheriting whatever
# `az login` session happens to be active. If the CLI is signed in to a
# different tenant, plan fails instead of targeting it. This project only ever
# runs against a lab tenant.

provider "azurerm" {
  features {}

  tenant_id       = var.tenant_id
  subscription_id = var.subscription_id

  # The storage account has account keys disabled, so any data-plane call the
  # provider makes has to use Entra ID auth as well.
  storage_use_azuread = true
}

# Used for the two resources azurerm can't manage correctly when the storage
# account has account keys disabled: the audit table (storage.tf) and the
# Function app (function.tf).
provider "azapi" {
  tenant_id       = var.tenant_id
  subscription_id = var.subscription_id
}

# Entra ID objects: the groups the pipeline manages membership of.
provider "azuread" {
  tenant_id = var.tenant_id
}
