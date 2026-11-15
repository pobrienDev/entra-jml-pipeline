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

# The tenant's default domain.
data "azuread_domains" "default" {
  only_default = true
}

locals {
  # Every setting the Function app has. Nothing else is added.
  function_app_settings = {
    # Host storage (leases, timers, the queue trigger) through the managed
    # identity. These three settings replace the usual AzureWebJobsStorage
    # connection string.
    "AzureWebJobsStorage__accountName" = azurerm_storage_account.main.name
    "AzureWebJobsStorage__credential"  = "managedidentity"
    "AzureWebJobsStorage__clientId"    = azurerm_user_assigned_identity.pipeline.client_id

    # Telemetry goes to Application Insights with the managed identity too.
    # The connection string says where to send it; it is not a credential,
    # because the resource only accepts Entra ID-authenticated callers.
    "APPLICATIONINSIGHTS_CONNECTION_STRING"     = azurerm_application_insights.main.connection_string
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
    "JML_UPN_DOMAIN"  = data.azuread_domains.default.domains[0].domain_name
    "JML_DRY_RUN"     = tostring(var.dry_run)
    "JML_AUDIT_TABLE" = azapi_resource.audit_table.name
    # The mass-change brake: refuse an export that would offboard more than
    # this share of the people the pipeline knows.
    "JML_MAX_LEAVE_PERCENT" = tostring(var.max_leave_percent)
  }
}

# The Function app that hosts the parser and processor. This creates the app;
# the code is deployed separately (scripts/deploy.sh).
#
# Created through the Azure management API (azapi) rather than
# azurerm_function_app_flex_consumption. That resource always writes an
# AzureWebJobsStorage connection string into the app's settings, built from
# an account key. In managed-identity mode there is no key, so it writes one
# with an empty key, which takes precedence over the identity settings above
# and stops the host reaching its own storage (403 on every start). Going
# through the management API, the app gets exactly the settings listed in
# local.function_app_settings.
resource "azapi_resource" "function_app" {
  type      = "Microsoft.Web/sites@2024-04-01"
  name      = "func-${var.name_prefix}-${random_string.suffix.result}"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location
  tags      = local.tags

  # Azure hands back resource IDs with different capitalisation than it
  # accepts ("serverfarms" for "serverFarms"). Without this, every plan would
  # report that difference as a change.
  ignore_casing = true

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.pipeline.id]
  }

  body = {
    kind = "functionapp,linux"
    properties = {
      serverFarmId = azurerm_service_plan.main.id
      httpsOnly    = true

      functionAppConfig = {
        runtime = {
          name    = "python"
          version = var.python_version
        }

        # Lab sizing: smallest instance, and a low ceiling on scale-out so a
        # bad file can't fan out into hundreds of instances hammering
        # Microsoft Graph.
        scaleAndConcurrency = {
          instanceMemoryMB     = 512
          maximumInstanceCount = 5
        }

        # Deployment storage: the app pulls its code package from this
        # container using the managed identity, not an account key.
        deployment = {
          storage = {
            type  = "blobContainer"
            value = "${azurerm_storage_account.main.primary_blob_endpoint}${azurerm_storage_container.deployments.name}"
            authentication = {
              type                           = "UserAssignedIdentity"
              userAssignedIdentityResourceId = azurerm_user_assigned_identity.pipeline.id
            }
          }
        }
      }

      siteConfig = {
        minTlsVersion = "1.2"
        appSettings = [
          for name, value in local.function_app_settings : { name = name, value = value }
        ]
      }
    }
  }

  # The app checks its storage on startup, so the identity's roles must exist
  # first.
  depends_on = [
    azurerm_role_assignment.identity_storage,
    azurerm_role_assignment.identity_telemetry,
  ]
}

# The app used to be an azurerm resource. This tells Terraform it is the same
# app, so existing environments are updated in place instead of rebuilt.
moved {
  from = azurerm_function_app_flex_consumption.main
  to   = azapi_resource.function_app
}
