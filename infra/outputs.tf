output "resource_group_name" {
  description = "Resource group holding everything this project creates."
  value       = azurerm_resource_group.main.name
}

output "function_app_name" {
  description = "Function app that hosts the parser and processor."
  value       = azapi_resource.function_app.name
}

output "storage_account_name" {
  description = "Storage account holding the HR feed, event queue and audit table."
  value       = azurerm_storage_account.main.name
}

output "hr_feed_container" {
  description = "Blob container HR CSV exports are uploaded to."
  value       = azurerm_storage_container.hr_feed.name
}

output "managed_identity_name" {
  description = "The identity the pipeline runs as."
  value       = azurerm_user_assigned_identity.pipeline.name
}

output "managed_identity_principal_id" {
  description = "Object ID of the pipeline identity's service principal. scripts/grant-graph-perms.sh grants Microsoft Graph permissions to this."
  value       = azurerm_user_assigned_identity.pipeline.principal_id
}

output "managed_identity_client_id" {
  description = "Client ID of the pipeline identity."
  value       = azurerm_user_assigned_identity.pipeline.client_id
}

output "deploy_identity_client_id" {
  description = "Client ID of the identity GitHub Actions deploys as. Goes in the lab environment's AZURE_CLIENT_ID secret."
  value       = azurerm_user_assigned_identity.deploy.client_id
}
