# The Entra ID groups the pipeline puts people in.
#
# config/department_groups.json is the single source of truth: the pipeline
# reads it to decide who belongs where, and this file reads it to make sure
# every group it names exists. Adding a department is one edit to that file.
#
# Terraform owns the groups; the pipeline owns their membership. Members are
# deliberately not managed here, so an apply never undoes what the pipeline
# did.

locals {
  department_map = jsondecode(file("${path.module}/../config/department_groups.json"))

  managed_group_names = toset(concat(
    local.department_map.all_employees,
    flatten(values(local.department_map.departments)),
    flatten(values(local.department_map.titles)),
  ))
}

resource "azuread_group" "managed" {
  for_each = local.managed_group_names

  display_name     = each.value
  description      = "Managed by entra-jml-pipeline (lab). Membership is set by the pipeline from the HR feed."
  security_enabled = true
  mail_enabled     = false

  # The pipeline finds groups by name, so a second group with the same name
  # would leave it guessing. Refuse to create one.
  prevent_duplicate_names = true

  lifecycle {
    # Every group must carry this project's prefix, so the map can never
    # name a group that belongs to another project sharing the tenant.
    precondition {
      condition     = startswith(each.value, "${var.name_prefix}-")
      error_message = "Group names in config/department_groups.json must start with \"${var.name_prefix}-\"."
    }
  }
}
