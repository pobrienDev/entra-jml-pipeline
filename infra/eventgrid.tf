# What turns an upload into a run.
#
# The system topic is the storage account's stream of events. The
# subscription picks out one kind (a CSV created in the feed container) and
# delivers it to the parser function.
#
# The endpoint is the function's Azure resource ID, not a webhook URL, so
# there is no function key to store: Event Grid is authorised through Azure
# itself.

resource "azurerm_eventgrid_system_topic" "storage" {
  name                = "evgt-${var.name_prefix}-storage"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  source_resource_id  = azurerm_storage_account.main.id
  topic_type          = "Microsoft.Storage.StorageAccounts"
  tags                = local.tags
}

resource "azurerm_eventgrid_system_topic_event_subscription" "hr_feed" {
  count = var.event_subscription_enabled ? 1 : 0

  name                = "hr-feed-to-parser"
  system_topic        = azurerm_eventgrid_system_topic.storage.name
  resource_group_name = azurerm_resource_group.main.name

  # BlobCreated fires once a blob is fully written, not while it uploads.
  included_event_types = ["Microsoft.Storage.BlobCreated"]

  subject_filter {
    subject_begins_with = "/blobServices/default/containers/${azurerm_storage_container.hr_feed.name}/blobs/"
    subject_ends_with   = ".csv"
  }

  azure_function_endpoint {
    function_id          = "${azapi_resource.function_app.id}/functions/parser"
    max_events_per_batch = 1
    # Azure's default, stated so the plan doesn't report it as a change.
    preferred_batch_size_in_kilobytes = 64
  }

  # If the parser is down or failing, keep trying for an hour, then give up.
  # The export stays in the container either way and can be uploaded again.
  retry_policy {
    max_delivery_attempts = 10
    event_time_to_live    = 60
  }
}
