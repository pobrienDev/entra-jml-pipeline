# Flex Consumption: scales to zero, billed per execution. A plan of this type
# hosts exactly one Function app.
resource "azurerm_service_plan" "main" {
  name                = "asp-${var.name_prefix}-pipeline"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = "FC1"
  tags                = local.tags
}

# The Function app that will host the parser and processor functions. This
# creates the empty app; the code is deployed separately.
resource "azurerm_function_app_flex_consumption" "main" {
  name                = "func-${var.name_prefix}-${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  service_plan_id     = azurerm_service_plan.main.id
  https_only          = true
  tags                = local.tags

  runtime_name    = "python"
  runtime_version = var.python_version

  # Lab sizing: smallest instance, and a low ceiling on scale-out so a bad
  # file can't fan out into hundreds of instances hammering Microsoft Graph.
  instance_memory_in_mb  = 512
  maximum_instance_count = 5

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.pipeline.id]
  }

  # Deployment storage: the app pulls its code package from this container
  # using the managed identity, not an account key.
  storage_container_type            = "blobContainer"
  storage_container_endpoint        = "${azurerm_storage_account.main.primary_blob_endpoint}${azurerm_storage_container.deployments.name}"
  storage_authentication_type       = "UserAssignedIdentity"
  storage_user_assigned_identity_id = azurerm_user_assigned_identity.pipeline.id

  site_config {
    application_insights_connection_string = azurerm_application_insights.main.connection_string
  }

  app_settings = {
    # Host storage (leases, timers, the queue trigger) through the managed
    # identity. These three settings replace the usual AzureWebJobsStorage
    # connection string.
    "AzureWebJobsStorage__accountName" = azurerm_storage_account.main.name
    "AzureWebJobsStorage__credential"  = "managedidentity"
    "AzureWebJobsStorage__clientId"    = azurerm_user_assigned_identity.pipeline.client_id

    # Send telemetry to Application Insights with the managed identity too.
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING" = "ClientId=${azurerm_user_assigned_identity.pipeline.client_id};Authorization=AAD"

    # Tells the Azure SDK in the function code which identity to use.
    "AZURE_CLIENT_ID" = azurerm_user_assigned_identity.pipeline.client_id

    # What the pipeline's own code reads. Names only, no credentials: it
    # reaches all of these as the managed identity.
    "JML_STORAGE_ACCOUNT" = azurerm_storage_account.main.name
    "JML_FEED_CONTAINER"  = azurerm_storage_container.hr_feed.name
    "JML_STATE_CONTAINER" = azurerm_storage_container.state.name
    "JML_QUEUE_NAME"      = azurerm_storage_queue.events.name
    # The domain new accounts get. Looked up from the tenant so it is never
    # written down in this repo.
    "JML_UPN_DOMAIN" = data.azuread_domains.default.domains[0].domain_name
    "JML_DRY_RUN"    = tostring(var.dry_run)
  }

  # The app checks its storage on startup, so the identity's roles must exist
  # first.
  depends_on = [
    azurerm_role_assignment.identity_storage,
    azurerm_role_assignment.identity_telemetry,
  ]
}

# The tenant's default domain.
data "azuread_domains" "default" {
  only_default = true
}
