"""Authentication and Microsoft Graph API calls.

All HTTP against Microsoft Graph lives in this module; nothing else in the
pipeline talks to the network directly.

Adapted from graph_api.py in employee-provisioning-tool. The request, retry,
paging and error handling and the user, group and license calls are copied
unchanged. What differs:

- Auth. The CLI authenticates with a client secret from a .env file. This
  client is handed a credential object instead and asks it for a token, so
  there is no secret anywhere: in Azure the credential is the Function app's
  managed identity, on a developer machine it is the Azure CLI sign-in (see
  auth.py).
- Left behind: the delegated sign-in client and everything that works on a
  mailbox (drafts, attachments, signature capture), and the MFA-method reset.
  They need a signed-in person or belong to features this service doesn't
  have.
- Added: lookups by employeeId and by group name, and manager get/set, at the
  bottom of the class.
"""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from azure.core.exceptions import ClientAuthenticationError

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class GraphError(Exception):
    """A Graph API call failed; the message carries the API's own error.

    `status` is the HTTP status and `code` Graph's error code (for example
    Authorization_RequestDenied), so callers can branch on the code rather
    than on message text wherever Graph gives a specific one. Several
    everyday failures — a duplicate UPN, a member already in a group, no
    seats left — arrive under the generic Request_BadRequest code, so for
    those the message text remains the only signal.
    """

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class GraphClient:
    """App-only Microsoft Graph client with token caching."""

    # How many times a request is attempted before its last answer is
    # raised, and the longest a single Retry-After is honored for.
    max_attempts = 3
    max_retry_wait = 120

    def __init__(self, credential):
        """`credential` is anything with azure-identity's get_token(scope):
        a ManagedIdentityCredential in Azure, an AzureCliCredential locally."""
        self.credential = credential
        self.session = requests.Session()
        self._token = None
        self._token_expires = 0.0

    def _get_token(self):
        """Return a valid access token, requesting a new one when needed."""
        if self._token and time.time() < self._token_expires:
            return self._token

        try:
            token = self.credential.get_token(GRAPH_SCOPE)
        except ClientAuthenticationError as exc:
            raise GraphError(f"could not get a Microsoft Graph token: {exc}") from exc

        self._token = token.token
        # Refresh a minute early so a token can't expire mid-request.
        self._token_expires = float(token.expires_on) - 60
        return self._token

    def _request(self, method, path, idempotent=True, **kwargs):
        """Send an authenticated request to Graph and return the response.

        `path` is either a path under the v1.0 base ("/users") or a full URL
        (as returned in @odata.nextLink paging links).

        Throttling and outage responses are retried, except that a request
        which is not idempotent (creating a user) is never
        resent after a 503/504: the gateway may have timed out *after* the
        directory applied the create, and a blind retry would then fail on
        "already exists" with no sign that the first attempt went through.
        """
        url = path if path.startswith("https://") else f"{GRAPH_BASE}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._get_token()}"
        for attempt in range(self.max_attempts):
            last = attempt == self.max_attempts - 1
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=30, **kwargs
                )
            except requests.RequestException as exc:
                # A reset connection or read timeout mid-offboarding would
                # otherwise stop the run; resend when a resend is harmless.
                if idempotent and not last:
                    time.sleep(self._retry_delay(None, attempt))
                    continue
                raise GraphError(f"{method} {url} failed: {exc}") from exc
            if not last and self._transient(response, idempotent):
                time.sleep(self._retry_delay(response, attempt))
                continue
            break
        if response.status_code >= 400:
            code, message = self._error_details(response)
            if not idempotent and response.status_code in (503, 504):
                message += (
                    " — not retried: the request may already have been applied, "
                    "so check before running it again"
                )
            raise GraphError(message, status=response.status_code, code=code)
        return response

    def _retry_delay(self, response, attempt):
        """Seconds to wait before the next attempt.

        Honors Retry-After as either a number of seconds or an HTTP date,
        falls back to a short linear backoff, and caps the wait so a
        far-future header can't hang the run.
        """
        header = (response.headers.get("Retry-After", "") if response is not None else "").strip()
        delay = None
        if header:
            try:
                delay = int(header)
            except ValueError:
                try:
                    when = parsedate_to_datetime(header)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = (when - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    delay = None
        if delay is None:
            delay = 2 * (attempt + 1)
        return max(1, min(delay, self.max_retry_wait))

    @staticmethod
    def _transient(response, idempotent=True):
        """True for throttling/outage responses Graph tells clients to retry.

        A 429 means the request was not processed, and a concurrency
        conflict means the write was rejected, so both are always safe to
        resend. A 503/504 is ambiguous — the request may have gone through
        — so only idempotent requests retry on those.
        """
        if response.status_code == 429:
            return True
        if response.status_code in (503, 504):
            return idempotent
        # Rapid writes to the same directory object can collide transiently.
        return (
            response.status_code == 409
            and "Directory_ConcurrencyViolation" in response.text
        )

    @staticmethod
    def _error_details(response):
        """(code, message) from a Graph error response; code is None when
        the body isn't Graph's usual error shape."""
        try:
            error = response.json()["error"]
            code = error.get("code")
            detail = f"{code}: {error.get('message')}"
        except (ValueError, KeyError, TypeError, AttributeError):
            code, detail = None, response.text
        return code, f"Graph API error ({response.status_code}) — {detail}"

    def _get_all(self, path, params=None):
        """GET a collection, following @odata.nextLink to the end.

        Graph pages most collections (users at 100 by default), so every
        list call goes through here rather than reading the first page and
        calling it the whole answer.
        """
        items = []
        url = path
        while url:
            page = self._request("GET", url, params=params).json()
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
            params = None   # a nextLink carries its own query string
        return items

    def list_users(self):
        """Return all users in the tenant, following paging links."""
        return self._get_all("/users?$select=displayName,userPrincipalName,jobTitle,accountEnabled")

    def address_holder(self, local, domain):
        """Who already receives mail at local@domain, or None.

        A UPN lookup matches UPNs and object IDs only. Exchange also
        delivers to every alias in proxyAddresses, to a group's address,
        and routes on mailNickname. So before a UPN is called free,
        users and groups are checked for the address in
        mail or proxyAddresses (either case of the smtp: prefix) and for a
        matching mailNickname. Returns a label naming the holder.
        """
        address = f"{local}@{domain}".replace("'", "''")
        nickname = local.replace("'", "''")
        clauses = (
            f"mail eq '{address}' or mailNickname eq '{nickname}' "
            f"or proxyAddresses/any(p:p eq 'smtp:{address}') "
            f"or proxyAddresses/any(p:p eq 'SMTP:{address}')"
        )
        for kind, path, select in (
            ("user", "/users", "displayName,userPrincipalName"),
            ("group", "/groups", "displayName,mail"),
        ):
            params = {"$filter": clauses, "$select": select, "$top": "1"}
            found = self._request("GET", path, params=params).json().get("value", [])
            if found:
                item = found[0]
                where = item.get("userPrincipalName") or item.get("mail") or kind
                return f"{item.get('displayName') or kind}, {where}"
        return None

    def get_user(self, upn_or_id, select):
        """Return one user, or None if no such account exists."""
        try:
            path = f"/users/{quote(upn_or_id, safe='')}?$select={select}"
            return self._request("GET", path).json()
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    def create_user(self, payload):
        """Create a user and return the new account object.

        Not retried on 503/504 (see _request): a second POST after a create
        that actually went through would leave an account whose temporary
        password was never shown to anyone.
        """
        return self._request("POST", "/users", idempotent=False, json=payload).json()

    def update_user(self, user_id, changes):
        """PATCH attributes on an existing user."""
        self._request("PATCH", f"/users/{user_id}", json=changes)

    def revoke_sessions(self, user_id):
        """Invalidate the user's sign-in sessions and refresh tokens."""
        self._request("POST", f"/users/{user_id}/revokeSignInSessions")

    def assign_license(self, user_id, sku_id):
        """Assign a license SKU to the user (account needs a usageLocation)."""
        payload = {"addLicenses": [{"skuId": sku_id}], "removeLicenses": []}
        self._request("POST", f"/users/{user_id}/assignLicense", json=payload)

    def list_skus(self):
        """Return the tenant's subscribed license SKUs."""
        return self._get_all("/subscribedSkus")

    def get_group(self, group_id, select="displayName"):
        """Return a group, or None if no such group exists."""
        try:
            return self._request("GET", f"/groups/{group_id}?$select={select}").json()
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    def add_group_member(self, group_id, user_id):
        """Add the user to a group."""
        payload = {"@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}"}
        self._request("POST", f"/groups/{group_id}/members/$ref", json=payload)

    def remove_group_member(self, group_id, user_id):
        """Remove the user from a group."""
        self._request("DELETE", f"/groups/{group_id}/members/{user_id}/$ref")

    def _member_of(self, user_id, select, odata_type):
        """The user's direct memberships of one directoryObject type.

        memberOf returns groups, directory roles and administrative units
        together; callers ask for the kind they can act on.
        """
        return [
            item for item in self._get_all(f"/users/{user_id}/memberOf?$select={select}")
            if item.get("@odata.type") == odata_type
        ]

    def get_member_groups(self, user_id):
        """Return the groups the user belongs to, with enough of each
        group's shape (types, mail settings) to tell how to leave it."""
        return self._member_of(
            user_id,
            "id,displayName,groupTypes,mailEnabled,securityEnabled,mail",
            "#microsoft.graph.group",
        )

    def get_member_roles(self, user_id):
        """Return the directory roles the user holds (id, displayName).

        A role is a different kind of privilege than a group membership and
        removing one needs RoleManagement.ReadWrite.Directory, which this
        pipeline deliberately doesn't hold — so roles are reported, not removed.
        Without a role-reading permission Graph still returns the role, but
        with only its id.
        """
        return self._member_of(user_id, "id,displayName", "#microsoft.graph.directoryRole")

    def remove_licenses(self, user_id, sku_ids):
        """Remove license SKUs from the user."""
        payload = {"addLicenses": [], "removeLicenses": sku_ids}
        self._request("POST", f"/users/{user_id}/assignLicense", json=payload)

    # --- added for the pipeline (not in employee-provisioning-tool) ----------

    def find_users_by_employee_id(self, employee_id, select):
        """Every user whose employeeId matches exactly.

        employeeId is how the pipeline recognises a person: names change and
        can collide, the HR system's ID does not. Returns a list because the
        directory doesn't enforce uniqueness on the attribute; a caller that
        gets more than one back has found a problem, not an answer.
        """
        escaped = str(employee_id).replace("'", "''")
        params = {"$filter": f"employeeId eq '{escaped}'", "$select": select}
        return self._get_all("/users", params=params)

    def find_groups_by_name(self, display_name, select="id,displayName"):
        """Every group whose displayName matches exactly. Display names are
        not unique in Entra ID, so this too returns a list."""
        escaped = str(display_name).replace("'", "''")
        params = {"$filter": f"displayName eq '{escaped}'", "$select": select}
        return self._get_all("/groups", params=params)

    def get_tenant(self):
        """The tenant's display name and verified domains.

        Needs a directory-read permission the pipeline's managed identity
        deliberately doesn't hold. Only the local runner calls it, as the
        signed-in administrator, to confirm which tenant it is about to
        change.
        """
        found = self._get_all("/organization?$select=displayName,verifiedDomains")
        return found[0] if found else {}

    def get_manager(self, user_id):
        """The user's manager (id, userPrincipalName), or None if unset."""
        try:
            path = f"/users/{user_id}/manager?$select=id,userPrincipalName"
            return self._request("GET", path).json()
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    def set_manager(self, user_id, manager_id):
        """Set the user's manager. A PUT, so repeating it is harmless."""
        payload = {"@odata.id": f"{GRAPH_BASE}/users/{manager_id}"}
        self._request("PUT", f"/users/{user_id}/manager/$ref", json=payload)
