# No defaults on the ID variables, on purpose: values live in the gitignored
# terraform.tfvars, so nothing tenant-specific is ever committed.

variable "tenant_id" {
  description = "Lab Entra ID tenant to provision into. Never a work tenant."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", lower(var.tenant_id)))
    error_message = "tenant_id must be a GUID."
  }
}

variable "subscription_id" {
  description = "Azure subscription to provision into."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", lower(var.subscription_id)))
    error_message = "subscription_id must be a GUID."
  }
}

variable "location" {
  description = "Azure region for all resources. Must be one where Flex Consumption is available."
  type        = string
  default     = "westus2"
}

variable "name_prefix" {
  description = "Short prefix applied to every resource name, so nothing here collides with the other projects sharing the lab tenant."
  type        = string
  default     = "jml"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,7}$", var.name_prefix))
    error_message = "name_prefix must be 2-8 lowercase letters or digits, starting with a letter (it is used in a storage account name)."
  }
}

variable "python_version" {
  description = "Python version the Function app runs."
  type        = string
  default     = "3.12"
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the resource group. Subscription owners are emailed at 80% of actual and 100% of forecast spend. Set to 0 to skip creating the budget."
  type        = number
  default     = 5
}

variable "dry_run" {
  description = "When true, the processor reads the directory and logs what it would do, and changes nothing."
  type        = bool
  default     = false
}

variable "event_subscription_enabled" {
  description = "Create the Event Grid subscription that calls the parser function. Event Grid checks that the function exists, so on a brand-new environment apply once with this set to false, deploy the code, then apply again."
  type        = bool
  default     = true
}

variable "feed_uploader_object_id" {
  description = "Object ID of the user or identity allowed to upload HR exports to the feed container. Null means whoever runs Terraform."
  type        = string
  default     = null
}
