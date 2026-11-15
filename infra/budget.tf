# Safety net. At lab volumes this project should cost close to nothing; the
# budget is there to say so loudly if that stops being true. It alerts, it
# does not stop spending.
resource "azurerm_consumption_budget_resource_group" "main" {
  count = var.monthly_budget_usd > 0 ? 1 : 0

  name              = "budget-${var.name_prefix}-pipeline"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.monthly_budget_usd
  time_grain        = "Monthly"

  time_period {
    # Azure requires the first day of a month, no earlier than the current
    # one. Computed once at creation and then left alone (see lifecycle).
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    operator       = "GreaterThanOrEqualTo"
    threshold      = 80
    threshold_type = "Actual"
    contact_roles  = ["Owner"]
  }

  notification {
    operator       = "GreaterThanOrEqualTo"
    threshold      = 100
    threshold_type = "Forecasted"
    contact_roles  = ["Owner"]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}
