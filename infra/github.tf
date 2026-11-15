# The identity GitHub Actions deploys as.
#
# It has no secret. GitHub mints a short-lived OIDC token for each workflow
# run; the federated credential below tells Azure which of those tokens to
# trust, and Azure exchanges a trusted one for an access token. There is
# nothing to store in the repository, rotate or leak.
#
# It is a second identity on purpose, separate from the one the pipeline runs
# as. The pipeline's identity can create and disable accounts across the
# tenant. This one can deploy code to a single Function app and do nothing
# else: no Microsoft Graph permissions, no storage access, no rights over the
# infrastructure. Terraform is applied by a person.

resource "azurerm_user_assigned_identity" "deploy" {
  name                = "id-${var.name_prefix}-deploy"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags
}

# Trust exactly one thing: a job in this repository's deployment environment.
#
# A job that declares `environment: lab` gets this subject; any other job in
# the repository gets a branch or pull-request subject and is refused. So the
# environment's protection rules gate every use of this identity.
#
# The subject uses GitHub's immutable format, with numeric owner and
# repository IDs. A repository *name* can be re-registered by someone else
# after a rename or deletion and would inherit the trust; the IDs can't be.
# Read a repository's prefix with:
#   gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
resource "azurerm_federated_identity_credential" "github_deploy" {
  name                      = "github-${var.github_environment}-environment"
  user_assigned_identity_id = azurerm_user_assigned_identity.deploy.id
  issuer                    = "https://token.actions.githubusercontent.com"
  audience                  = ["api://AzureADTokenExchange"]
  subject                   = "${var.github_subject_prefix}:environment:${var.github_environment}"
}

# Deploying code is a write to the Function app, and that is all this grants:
# Website Contributor on the one app, not on the resource group.
resource "azurerm_role_assignment" "deploy_function_app" {
  scope                = azapi_resource.function_app.id
  role_definition_name = "Website Contributor"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
  principal_type       = "ServicePrincipal"
}
