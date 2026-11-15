"""Tests for shared.graph_api.GraphClient: tokens, retries, errors, and paging.

No network: the client's requests.Session is replaced by FakeSession, which
hands back scripted responses in order and records every request made, and
its credential by FakeCredential, which hands out scripted tokens.
time.sleep is replaced too, so the retry tests run instantly and can assert
exactly how long the client *would* have waited.

Copied from employee-provisioning-tool. Tests for what was left behind (the
client-secret flow, the delegated sign-in, mailbox drafts, MFA reset) are
gone; the authentication tests are rewritten for a credential object; tests
for the methods added here are at the bottom.

Run from the repo root:  python -m pytest -q
"""

from collections import namedtuple

import pytest
from azure.core.exceptions import ClientAuthenticationError

from shared import graph_api
from shared.graph_api import GRAPH_BASE, GRAPH_SCOPE, GraphClient, GraphError


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


AccessToken = namedtuple("AccessToken", "token expires_on")


class FakeCredential:
    """Scripted stand-in for an azure-identity credential.

      tokens      AccessTokens (or exceptions to raise) handed out in order
      .scopes     the scope of every token request
    """

    def __init__(self, tokens=None):
        self.tokens = list(tokens or [AccessToken("tok-1", 4_000_000_000)])
        self.scopes = []

    def get_token(self, scope):
        self.scopes.append(scope)
        token = self.tokens.pop(0)
        if isinstance(token, Exception):
            raise token
        return token


class FakeSession:
    """Scripted stand-in for requests.Session.

      responses         answers for Graph requests, in order
      .requests         every Graph request as (method, url, headers, kwargs)
    """

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, dict(headers or {}), kwargs))
        return self.responses.pop(0)


@pytest.fixture
def sleeps(monkeypatch):
    """Record time.sleep calls instead of waiting."""
    recorded = []
    monkeypatch.setattr(graph_api.time, "sleep", recorded.append)
    return recorded


def make_client(responses=(), tokens=None):
    client = GraphClient(FakeCredential(tokens))
    client.session = FakeSession(responses)
    return client


# --- authentication -----------------------------------------------------------

def test_token_comes_from_the_credential_for_the_graph_scope():
    client = make_client([FakeResponse(200, {"value": []})])

    client.list_skus()

    assert client.credential.scopes == [GRAPH_SCOPE]
    assert GRAPH_SCOPE == "https://graph.microsoft.com/.default"
    assert client.session.requests[0][2]["Authorization"] == "Bearer tok-1"


def test_token_is_cached_across_requests():
    client = make_client([FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})])

    client.list_skus()
    client.list_skus()

    assert len(client.credential.scopes) == 1


def test_expired_token_is_refreshed():
    client = make_client(
        [FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})],
        tokens=[AccessToken("tok-1", 4_000_000_000), AccessToken("tok-2", 4_000_000_000)],
    )
    client.list_skus()

    client._token_expires = 0.0   # as if the hour had passed
    client.list_skus()

    assert len(client.credential.scopes) == 2
    assert client.session.requests[1][2]["Authorization"] == "Bearer tok-2"


def test_token_is_refreshed_a_minute_early():
    client = make_client(tokens=[AccessToken("tok-1", 5000)])

    client._get_token()

    assert client._token_expires == 5000 - 60


def test_a_credential_that_cannot_authenticate_raises_graph_error():
    client = make_client(tokens=[ClientAuthenticationError("no managed identity endpoint found")])

    with pytest.raises(GraphError, match="could not get a Microsoft Graph token"):
        client.list_skus()

    assert client.session.requests == []   # nothing was sent without a token


# --- retries ------------------------------------------------------------------

def test_throttling_waits_out_retry_after(sleeps):
    client = make_client([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"value": []}),
    ])

    client.list_skus()

    assert sleeps == [7]
    assert len(client.session.requests) == 2


def test_outage_without_retry_after_backs_off(sleeps):
    client = make_client([FakeResponse(503), FakeResponse(504), FakeResponse(200, {"value": []})])

    client.list_skus()

    assert sleeps == [2, 4]


def test_directory_concurrency_collision_is_retried(sleeps):
    collision = FakeResponse(409, text='{"error":{"code":"Directory_ConcurrencyViolation"}}')
    client = make_client([collision, FakeResponse(204)])

    client.update_user("user-1", {"jobTitle": "Manager"})

    assert len(client.session.requests) == 2


def test_an_ordinary_conflict_is_not_retried(sleeps):
    conflict = FakeResponse(409, {"error": {"code": "Request_BadRequest", "message": "already exists"}})
    client = make_client([conflict])

    with pytest.raises(GraphError, match="Request_BadRequest: already exists"):
        client.update_user("user-1", {"jobTitle": "Manager"})

    assert sleeps == []
    assert len(client.session.requests) == 1


def test_gives_up_after_three_attempts(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "1"})] * 3)

    with pytest.raises(GraphError) as excinfo:
        client.list_skus()

    assert excinfo.value.status == 429
    assert len(client.session.requests) == 3
    assert len(sleeps) == 2   # no pointless wait after the final attempt


# --- errors -------------------------------------------------------------------

def test_graph_error_carries_code_message_and_status():
    denied = FakeResponse(403, {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}})
    client = make_client([denied])

    with pytest.raises(GraphError) as excinfo:
        client.revoke_sessions("user-1")

    assert excinfo.value.status == 403
    assert excinfo.value.code == "Authorization_RequestDenied"
    assert "Authorization_RequestDenied: Insufficient privileges" in str(excinfo.value)


def test_non_json_error_body_falls_back_to_text():
    client = make_client([FakeResponse(500, text="upstream gateway exploded")])

    with pytest.raises(GraphError, match="upstream gateway exploded") as excinfo:
        client.list_skus()

    assert excinfo.value.code is None


# --- the calls offboarding depends on -----------------------------------------

def test_member_groups_follows_paging_and_keeps_only_groups():
    next_link = f"{GRAPH_BASE}/users/user-1/memberOf?$skiptoken=abc"
    client = make_client([
        FakeResponse(200, {
            "value": [
                {"@odata.type": "#microsoft.graph.group", "id": "g1"},
                {"@odata.type": "#microsoft.graph.directoryRole", "id": "role"},
            ],
            "@odata.nextLink": next_link,
        }),
        FakeResponse(200, {"value": [{"@odata.type": "#microsoft.graph.group", "id": "g2"}]}),
    ])

    groups = client.get_member_groups("user-1")

    assert [group["id"] for group in groups] == ["g1", "g2"]
    # The nextLink is a full URL and must be used as-is, not re-prefixed.
    assert client.session.requests[1][1] == next_link


def test_lockout_calls_hit_the_right_endpoints():
    client = make_client([FakeResponse(204), FakeResponse(200, {"value": True})])

    client.update_user("user-1", {"accountEnabled": False})
    client.revoke_sessions("user-1")

    (m1, u1, _, k1), (m2, u2, _, _) = client.session.requests
    assert (m1, u1, k1["json"]) == ("PATCH", f"{GRAPH_BASE}/users/user-1", {"accountEnabled": False})
    assert (m2, u2) == ("POST", f"{GRAPH_BASE}/users/user-1/revokeSignInSessions")


def test_remove_licenses_sends_only_removals():
    client = make_client([FakeResponse(200, {})])

    client.remove_licenses("user-1", ["sku-a", "sku-b"])

    method, url, _, kwargs = client.session.requests[0]
    assert (method, url) == ("POST", f"{GRAPH_BASE}/users/user-1/assignLicense")
    assert kwargs["json"] == {"addLicenses": [], "removeLicenses": ["sku-a", "sku-b"]}


def test_remove_group_member_targets_the_membership_reference():
    client = make_client([FakeResponse(204)])

    client.remove_group_member("group-1", "user-1")

    method, url, _, _ = client.session.requests[0]
    assert (method, url) == ("DELETE", f"{GRAPH_BASE}/groups/group-1/members/user-1/$ref")



def test_member_roles_keeps_only_directory_roles():
    client = make_client([
        FakeResponse(200, {
            "value": [
                {"@odata.type": "#microsoft.graph.group", "id": "g1"},
                {"@odata.type": "#microsoft.graph.directoryRole", "id": "role", "displayName": "User Administrator"},
                {"@odata.type": "#microsoft.graph.administrativeUnit", "id": "au"},
            ],
        }),
    ])

    roles = client.get_member_roles("user-1")

    assert [role["id"] for role in roles] == ["role"]
    _, url, _, _ = client.session.requests[0]
    assert url == f"{GRAPH_BASE}/users/user-1/memberOf?$select=id,displayName"


# --- creates are never blindly retried ----------------------------------------

def test_create_user_is_not_retried_after_a_gateway_timeout(sleeps):
    client = make_client([FakeResponse(504, text="gateway timeout")])

    with pytest.raises(GraphError, match="may already have been applied") as excinfo:
        client.create_user({"userPrincipalName": "t@x"})

    assert excinfo.value.status == 504
    assert len(client.session.requests) == 1
    assert sleeps == []


def test_create_user_still_retries_throttling(sleeps):
    client = make_client([
        FakeResponse(429, headers={"Retry-After": "1"}),
        FakeResponse(201, {"id": "u1"}),
    ])

    assert client.create_user({"userPrincipalName": "t@x"}) == {"id": "u1"}
    assert len(client.session.requests) == 2


def test_idempotent_writes_still_retry_outages(sleeps):
    client = make_client([FakeResponse(503), FakeResponse(204)])

    client.update_user("user-1", {"jobTitle": "Manager"})

    assert len(client.session.requests) == 2


# --- address collisions beyond the UPN -----------------------------------------

def test_address_holder_checks_mail_aliases_and_nickname_on_users_then_groups():
    client = make_client([
        FakeResponse(200, {"value": []}),
        FakeResponse(200, {"value": [{"displayName": "Sales Team", "mail": "sales@d.com"}]}),
    ])

    assert client.address_holder("sales", "d.com") == "Sales Team, sales@d.com"

    (_, u1, _, k1), (_, u2, _, k2) = client.session.requests
    assert (u1, u2) == (f"{GRAPH_BASE}/users", f"{GRAPH_BASE}/groups")
    for kwargs in (k1, k2):
        flt = kwargs["params"]["$filter"]
        assert "mail eq 'sales@d.com'" in flt
        assert "mailNickname eq 'sales'" in flt
        assert "proxyAddresses/any(p:p eq 'smtp:sales@d.com')" in flt
        assert "proxyAddresses/any(p:p eq 'SMTP:sales@d.com')" in flt


def test_address_holder_names_the_user_holding_an_alias():
    client = make_client([
        FakeResponse(200, {"value": [{"displayName": "Front Desk", "userPrincipalName": "frontdesk@d.com"}]}),
    ])

    assert client.address_holder("tsmith", "d.com") == "Front Desk, frontdesk@d.com"
    assert len(client.session.requests) == 1   # found on users; groups not queried


def test_address_holder_escapes_apostrophes_in_the_filter():
    client = make_client([FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})])

    assert client.address_holder("o'brien", "d.com") is None

    assert "mailNickname eq 'o''brien'" in client.session.requests[0][3]["params"]["$filter"]


# --- retry gaps: connection errors, HTTP-date Retry-After, the wait cap ------------

import requests as _requests  # noqa: E402
from email.utils import format_datetime  # noqa: E402
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402


class FlakySession(FakeSession):
    """Raises a connection error for the first `failures` Graph requests."""

    def __init__(self, responses, failures):
        super().__init__(responses)
        self.failures = failures

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, dict(headers or {}), kwargs))
        if self.failures:
            self.failures -= 1
            raise _requests.ConnectionError("connection reset by peer")
        return self.responses.pop(0)


def test_a_dropped_connection_is_retried_for_idempotent_requests(sleeps):
    client = make_client()
    client.session = FlakySession([FakeResponse(200, {"value": []})], failures=1)

    client.list_skus()

    assert len(client.session.requests) == 2
    assert sleeps == [2]


def test_a_dropped_connection_is_not_retried_for_a_create(sleeps):
    client = make_client()
    client.session = FlakySession([FakeResponse(201, {"id": "u"})], failures=1)

    with pytest.raises(GraphError, match="connection reset"):
        client.create_user({"userPrincipalName": "t@x"})

    assert len(client.session.requests) == 1 and sleeps == []


def test_a_connection_that_never_recovers_gives_up_after_max_attempts(sleeps):
    client = make_client()
    client.session = FlakySession([], failures=99)

    with pytest.raises(GraphError):
        client.list_skus()

    assert len(client.session.requests) == 3


def test_retry_after_as_an_http_date_is_honored(sleeps):
    when = _dt.now(_tz.utc) + _td(seconds=30)
    client = make_client([
        FakeResponse(429, headers={"Retry-After": format_datetime(when, usegmt=True)}),
        FakeResponse(200, {"value": []}),
    ])

    client.list_skus()

    assert len(sleeps) == 1 and 25 <= sleeps[0] <= 30


def test_an_absurd_retry_after_is_capped(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "86400"}), FakeResponse(200, {"value": []})])

    client.list_skus()

    assert sleeps == [120]


def test_an_unparseable_retry_after_falls_back_to_backoff(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "soon"}), FakeResponse(200, {"value": []})])

    client.list_skus()

    assert sleeps == [2]


def test_attempt_count_is_configurable(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "1"})] * 5)
    client.max_attempts = 5

    with pytest.raises(GraphError):
        client.list_skus()

    assert len(client.session.requests) == 5


# --- every list call follows paging -------------------------------------------------

@pytest.mark.parametrize("call, path", [
    (lambda c: c.list_skus(), "/subscribedSkus"),
    (lambda c: c.list_users(), "/users"),
    (lambda c: c.find_users_by_employee_id("E1001", "id"), "/users"),
    (lambda c: c.find_groups_by_name("jml-all-staff"), "/groups"),
])
def test_list_calls_read_past_the_first_page(call, path):
    next_link = f"{GRAPH_BASE}{path}?$skiptoken=x"
    client = make_client([
        FakeResponse(200, {"value": [{"id": 1}], "@odata.nextLink": next_link}),
        FakeResponse(200, {"value": [{"id": 2}]}),
    ])

    assert [item["id"] for item in call(client)] == [1, 2]
    assert client.session.requests[1][1] == next_link


# --- methods added for the pipeline ---------------------------------------------------

def test_employee_id_lookup_filters_exactly_and_escapes_apostrophes():
    client = make_client([FakeResponse(200, {"value": [{"id": "u1"}]})])

    assert client.find_users_by_employee_id("E'1001", "id,employeeId") == [{"id": "u1"}]

    method, url, _, kwargs = client.session.requests[0]
    assert (method, url) == ("GET", f"{GRAPH_BASE}/users")
    assert kwargs["params"] == {"$filter": "employeeId eq 'E''1001'", "$select": "id,employeeId"}


def test_group_lookup_is_by_exact_display_name():
    client = make_client([FakeResponse(200, {"value": [{"id": "g1", "displayName": "jml-dept-sales"}]})])

    assert client.find_groups_by_name("jml-dept-sales") == [{"id": "g1", "displayName": "jml-dept-sales"}]

    _, url, _, kwargs = client.session.requests[0]
    assert url == f"{GRAPH_BASE}/groups"
    assert kwargs["params"]["$filter"] == "displayName eq 'jml-dept-sales'"


def test_a_user_with_no_manager_reads_as_none():
    missing = FakeResponse(404, {"error": {"code": "Request_ResourceNotFound", "message": "no manager"}})
    client = make_client([missing])

    assert client.get_manager("user-1") is None


def test_get_manager_returns_the_manager():
    client = make_client([FakeResponse(200, {"id": "boss-1", "userPrincipalName": "boss@d.com"})])

    assert client.get_manager("user-1")["id"] == "boss-1"
    assert client.session.requests[0][1] == f"{GRAPH_BASE}/users/user-1/manager?$select=id,userPrincipalName"


def test_set_manager_puts_a_reference_to_the_manager():
    client = make_client([FakeResponse(204)])

    client.set_manager("user-1", "boss-1")

    method, url, _, kwargs = client.session.requests[0]
    assert (method, url) == ("PUT", f"{GRAPH_BASE}/users/user-1/manager/$ref")
    assert kwargs["json"] == {"@odata.id": f"{GRAPH_BASE}/users/boss-1"}
