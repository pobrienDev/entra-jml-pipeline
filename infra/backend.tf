# Remote state in Azure Storage. Deliberately empty ("partial configuration"):
# the storage account name is supplied at init time from the gitignored
# backend.tfbackend, so it never lands in a public repo.
#
#   terraform init -backend-config=backend.tfbackend
#
# The storage account is the one the entra-terraform project bootstrapped for
# the lab tenant. This project keeps its own state file (its own blob key) in
# that container and never reads or writes another project's state.

terraform {
  backend "azurerm" {}
}
