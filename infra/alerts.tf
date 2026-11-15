# Tells a person when the pipeline has stopped short of doing its job.
#
# The Function app writes two log lines for exactly this purpose:
#
#   poison:  a message failed its fifth and last attempt and was moved to the
#            poison queue. One person's join, move or leave did not complete.
#   brake:   an HR export was refused because it would have offboarded too
#            many people at once. Nothing in that file was processed.
#
# The alert watches the logs rather than the poison queue's length because a
# queue-length metric is only reported for the storage account as a whole,
# hourly. A log line says which person and why, within minutes.

resource "azurerm_monitor_action_group" "alerts" {
  count = var.alert_email == null ? 0 : 1

  name                = "ag-${var.name_prefix}-pipeline"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "${var.name_prefix}-alerts"
  tags                = local.tags

  email_receiver {
    name          = "owner"
    email_address = var.alert_email
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "needs_attention" {
  name                = "alert-${var.name_prefix}-needs-attention"
  display_name        = "JML pipeline needs attention"
  description         = "A message reached the poison queue, or an HR export was refused by the mass-change brake."
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  scopes              = [azurerm_application_insights.main.id]
  severity            = 1
  tags                = local.tags

  evaluation_frequency = "PT15M"
  window_duration      = "PT15M"

  criteria {
    query                   = <<-QUERY
      traces
      | where message startswith "poison:" or message startswith "brake:"
      QUERY
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  # Clears itself once a window passes with nothing new to report.
  auto_mitigation_enabled = true

  action {
    action_groups = [for group in azurerm_monitor_action_group.alerts : group.id]
  }
}
