# One storage account holds the pipeline's data (HR feed, event queue, audit
# table) and the Function app's own host and deployment storage.
resource "azurerm_storage_account" "main" {
  name                     = "st${var.name_prefix}${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = local.tags

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false # no anonymous blob access

  # No account keys, so no connection strings exist to leak. Every caller,
  # including the Function app, authenticates with Entra ID and is authorised
  # by the role assignments below.
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
}

# Drop zone for the HR CSV export. A new blob here is what starts a run.
resource "azurerm_storage_container" "hr_feed" {
  name                  = "hr-feed"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

# Where Flex Consumption keeps the deployed code package.
resource "azurerm_storage_container" "deployments" {
  name                  = "deployments"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

# One message per change (join, move or leave), written by the parser and
# consumed by the processor.
resource "azurerm_storage_queue" "events" {
  name               = "jml-events"
  storage_account_id = azurerm_storage_account.main.id
}

# The Functions runtime moves a message here after it has failed five times,
# and would create the queue itself on first use. Declaring it means it exists
# from day one, so an alert can watch it.
resource "azurerm_storage_queue" "events_poison" {
  name               = "${azurerm_storage_queue.events.name}-poison"
  storage_account_id = azurerm_storage_account.main.id
}

# Audit log: who, what, when and the result, for every action the pipeline
# takes. Table names allow letters and digits only.
resource "azurerm_storage_table" "audit" {
  name               = "jmlaudit"
  storage_account_id = azurerm_storage_account.main.id
}

# Data-plane access for the pipeline identity. With account keys disabled,
# these role assignments are the only way in.
locals {
  identity_storage_roles = {
    # Blob Data Owner, not Contributor: Microsoft documents Owner as the
    # minimum for the Functions host's own storage connection
    # (AzureWebJobsStorage) when it uses an identity. It also covers the HR
    # feed and the deployment container.
    blob = "Storage Blob Data Owner"
    # Read, write and delete queue messages.
    queue = "Storage Queue Data Contributor"
    # The audit table, plus the host's own diagnostic events.
    table = "Storage Table Data Contributor"
  }
}

resource "azurerm_role_assignment" "identity_storage" {
  for_each = local.identity_storage_roles

  scope                = azurerm_storage_account.main.id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.pipeline.principal_id
  principal_type       = "ServicePrincipal"
}
