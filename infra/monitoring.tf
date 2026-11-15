# Application Insights stores its data in a Log Analytics workspace.
resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${var.name_prefix}-pipeline"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags

  # Cost brake: stop ingesting for the day after 1 GB. Lab volumes are a tiny
  # fraction of that, so hitting it means something is logging in a loop.
  daily_quota_gb = 1
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${var.name_prefix}-pipeline"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = local.tags

  # Telemetry is accepted only from callers that authenticate with Entra ID.
  # The connection string alone (which holds an instrumentation key) is not
  # enough to write here, so it is not a secret worth stealing.
  local_authentication_enabled = false
}

# Lets the pipeline identity send telemetry now that key-based ingestion is off.
resource "azurerm_role_assignment" "identity_telemetry" {
  scope                = azurerm_application_insights.main.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_user_assigned_identity.pipeline.principal_id
  principal_type       = "ServicePrincipal"
}
