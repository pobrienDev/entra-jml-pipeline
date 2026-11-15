"""Where the pipeline's Microsoft Graph token comes from.

There is no client secret and no .env file. The credential is chosen by where
the code is running:

- In Azure, the Function app's user-assigned managed identity. Azure issues
  the token to the app at runtime; nothing is stored.
- On a developer machine, whoever is signed in to the Azure CLI (`az login`).

DefaultAzureCredential would cover both, but it also tries credentials read
from environment variables, a client secret among them. Naming the two
allowed sources keeps "no stored secrets" a property of the code rather than
of how it happens to be configured.
"""

import os

from azure.identity import AzureCliCredential, ManagedIdentityCredential


def running_in_azure():
    """True inside App Service / Functions, which set IDENTITY_ENDPOINT for
    apps that have a managed identity."""
    return bool(os.environ.get("IDENTITY_ENDPOINT"))


def pipeline_credential():
    """The credential GraphClient should use in this environment."""
    if running_in_azure():
        # AZURE_CLIENT_ID picks the user-assigned identity; Terraform sets it.
        return ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
    return AzureCliCredential()
