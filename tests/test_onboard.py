"""Tests for the joiner: accounts, groups and managers, and above all that
running it twice changes nothing the second time.

No network: Microsoft Graph is replaced by FakeDirectory, a small in-memory
tenant that answers the reads the joiner makes and records every write.

Run from the repo root:  python -m pytest -q
"""

from collections import Counter

from shared.graph_api import GraphError
from shared.onboard import onboard, resolve_groups, run_joiners

CONFIG = {
    "tenant": {"domain": "d.com", "usage_location": "US"},
    "groups": {
        "all_employees": ["all-staff"],
        "departments": {"Engineering": ["dept-eng"], "Sales": ["dept-sales"]},
        "titles": {"Engineering Manager": ["managers"]},
    },
}
GROUPS = {"all-staff": "g-all", "dept-eng": "g-eng", "dept-sales": "g-sales", "managers": "g-mgr"}


class FakeDirectory:
    """In-memory stand-in for shared.graph_api.GraphClient.

      .users      object ID -> user dict
      .members    group ID -> set of user IDs
      .managers   user ID -> manager's user ID
      .writes     every write, in order, as (operation, ...) tuples
      fail_group  a group ID whose membership adds are refused
    """

    def __init__(self, groups=GROUPS, fail_group=None):
        self.groups = dict(groups)
        self.users = {}
        self.members = {group_id: set() for group_id in self.groups.values()}
        self.managers = {}
        self.writes = []
        self.payloads = []
        self.fail_group = fail_group

    # reads
    def get_user(self, upn, select):
        for user in self.users.values():
            if user["userPrincipalName"] == upn:
                return user
        return None

    def address_holder(self, local, domain):
        return None

    def find_users_by_employee_id(self, employee_id, select):
        return [u for u in self.users.values() if u.get("employeeId") == employee_id]

    def find_groups_by_name(self, name):
        return [{"id": gid, "displayName": n} for n, gid in self.groups.items() if n == name]

    def get_member_groups(self, user_id):
        return [{"id": gid} for gid, members in self.members.items() if user_id in members]

    def get_manager(self, user_id):
        manager_id = self.managers.get(user_id)
        return {"id": manager_id} if manager_id else None

    # writes
    def create_user(self, payload):
        user = {k: v for k, v in payload.items() if k != "passwordProfile"}
        user["id"] = f"u{len(self.users) + 1}"
        self.users[user["id"]] = user
        self.payloads.append(payload)
        self.writes.append(("create_user", payload["userPrincipalName"]))
        return user

    def add_group_member(self, group_id, user_id):
        if group_id == self.fail_group:
            raise GraphError("Graph API error (403) — Authorization_RequestDenied", status=403)
        self.members[group_id].add(user_id)
        self.writes.append(("add_group_member", group_id, user_id))

    def set_manager(self, user_id, manager_id):
        self.managers[user_id] = manager_id
        self.writes.append(("set_manager", user_id, manager_id))

    # helpers for assertions
    def user(self, employee_id):
        (found,) = self.find_users_by_employee_id(employee_id, "")
        return found

    def groups_of(self, employee_id):
        user_id = self.user(employee_id)["id"]
        names = {gid: name for name, gid in self.groups.items()}
        return sorted(names[gid] for gid, members in self.members.items() if user_id in members)


def employee(employee_id, first, last, department="Engineering", title="Software Engineer",
             manager_id="", start_date="", status="active"):
    return {"employee_id": employee_id, "first_name": first, "last_name": last,
            "department": department, "title": title, "manager_id": manager_id,
            "start_date": start_date, "status": status}


TEAM = [
    employee("E2", "Ada", "Byron", manager_id="E1"),          # listed before her manager
    employee("E1", "Grace", "Hopper", title="Engineering Manager"),
    employee("E3", "Alan", "Turing", department="Sales", title="Account Executive", manager_id="E1"),
]


def results(actions):
    return Counter(a["result"] for a in actions)


# --- a first run ----------------------------------------------------------------

def test_a_first_run_creates_accounts_with_groups_and_managers():
    directory = FakeDirectory()

    actions = run_joiners(directory, TEAM, CONFIG, GROUPS)

    assert results(actions) == {"done": 3 + 7 + 2}   # accounts, memberships, managers
    assert directory.user("E1")["userPrincipalName"] == "ghopper@d.com"
    assert directory.groups_of("E1") == ["all-staff", "dept-eng", "managers"]
    assert directory.groups_of("E2") == ["all-staff", "dept-eng"]
    assert directory.groups_of("E3") == ["all-staff", "dept-sales"]
    boss = directory.user("E1")["id"]
    assert directory.managers == {directory.user("E2")["id"]: boss, directory.user("E3")["id"]: boss}


def test_the_new_account_carries_the_hr_attributes():
    directory = FakeDirectory()

    onboard(directory, employee("E7", "Chloé", "Lefèvre", title="QA Engineer", start_date="2026-11-09"),
            CONFIG, GROUPS)

    payload = directory.payloads[0]
    assert payload["employeeId"] == "E7"
    assert payload["displayName"] == "Chloé Lefèvre"
    assert payload["userPrincipalName"] == "clefevre@d.com"      # accents reduced for the UPN only
    assert payload["mailNickname"] == "clefevre"
    assert payload["department"] == "Engineering"
    assert payload["jobTitle"] == "QA Engineer"
    assert payload["employeeHireDate"] == "2026-11-09T00:00:00Z"
    assert payload["usageLocation"] == "US"
    assert payload["accountEnabled"] is True
    assert payload["passwordProfile"]["forceChangePasswordNextSignIn"] is True


def test_the_temporary_password_never_appears_in_an_action_record():
    directory = FakeDirectory()

    _, actions = onboard(directory, employee("E7", "Ada", "Byron"), CONFIG, GROUPS)

    password = directory.payloads[0]["passwordProfile"]["password"]
    assert password and all(password not in str(value) for a in actions for value in a.values())


# --- idempotency ----------------------------------------------------------------

def test_a_second_run_of_the_same_file_changes_nothing():
    directory = FakeDirectory()
    run_joiners(directory, TEAM, CONFIG, GROUPS)
    writes_after_first = list(directory.writes)

    actions = run_joiners(directory, TEAM, CONFIG, GROUPS)

    assert directory.writes == writes_after_first
    assert results(actions) == {"skipped": 12}


def test_a_half_finished_run_is_completed_not_repeated():
    directory = FakeDirectory()
    ada = employee("E2", "Ada", "Byron")
    onboard(directory, ada, CONFIG, GROUPS)
    directory.members["g-eng"].clear()          # as if the run died before this add
    before = len(directory.writes)

    _, actions = onboard(directory, ada, CONFIG, GROUPS)

    assert directory.writes[before:] == [("add_group_member", "g-eng", directory.user("E2")["id"])]
    assert [(a["target"], a["result"]) for a in actions] == [
        ("abyron@d.com", "skipped"), ("all-staff", "skipped"), ("dept-eng", "done"),
    ]


def test_people_are_matched_on_employee_id_not_name():
    directory = FakeDirectory()
    run_joiners(directory, [employee("E2", "Ada", "Byron")], CONFIG, GROUPS)

    # Same person after a name change in HR: no second account.
    run_joiners(directory, [employee("E2", "Ada", "Lovelace")], CONFIG, GROUPS)
    # A different person who happens to share the original name: a new account.
    run_joiners(directory, [employee("E9", "Ada", "Byron")], CONFIG, GROUPS)

    assert len(directory.users) == 2
    assert directory.user("E2")["userPrincipalName"] == "abyron@d.com"
    assert directory.user("E9")["userPrincipalName"] == "adbyron@d.com"   # next rung of the ladder


def test_memberships_the_pipeline_did_not_grant_are_left_alone():
    directory = FakeDirectory(groups={**GROUPS, "someone-elses-group": "g-other"})
    ada = employee("E2", "Ada", "Byron")
    onboard(directory, ada, CONFIG, GROUPS)
    directory.members["g-other"].add(directory.user("E2")["id"])

    onboard(directory, ada, CONFIG, GROUPS)

    assert "someone-elses-group" in directory.groups_of("E2")


# --- dry run --------------------------------------------------------------------

def test_a_dry_run_writes_nothing_and_says_what_it_would_do():
    directory = FakeDirectory()

    actions = run_joiners(directory, TEAM, CONFIG, GROUPS, dry_run=True)

    assert directory.writes == [] and directory.users == {}
    assert results(actions) == {"would": 12}
    assert ("create_user", "ghopper@d.com") in [(a["action"], a["target"]) for a in actions]


def test_a_dry_run_over_existing_accounts_reports_only_the_difference():
    directory = FakeDirectory()
    run_joiners(directory, TEAM, CONFIG, GROUPS)
    directory.members["g-mgr"].clear()
    before = list(directory.writes)

    actions = run_joiners(directory, TEAM, CONFIG, GROUPS, dry_run=True)

    assert directory.writes == before
    assert [(a["employee_id"], a["target"]) for a in actions if a["result"] == "would"] == [("E1", "managers")]


# --- two people in one file wanting the same UPN ----------------------------------

LEES = [employee("E6", "Jordan", "Lee"), employee("E7", "Jamie", "Lee")]


def test_a_dry_run_does_not_offer_the_same_upn_to_two_people():
    actions = run_joiners(FakeDirectory(), LEES, CONFIG, GROUPS, dry_run=True)

    assert [a["target"] for a in actions if a["action"] == "create_user"] == ["jlee@d.com", "jalee@d.com"]


def test_a_just_created_account_the_directory_cannot_see_yet_still_counts_as_taken():
    class Lagging(FakeDirectory):
        """New accounts aren't readable by UPN straight away."""

        def get_user(self, upn, select):
            return None

    directory = Lagging()

    actions = run_joiners(directory, LEES, CONFIG, GROUPS)

    assert [w[1] for w in directory.writes if w[0] == "create_user"] == ["jlee@d.com", "jalee@d.com"]
    assert "failed" not in results(actions)


# --- failures stay with the person they belong to ---------------------------------

def test_a_refused_group_add_is_recorded_and_the_other_groups_still_happen():
    directory = FakeDirectory(fail_group="g-eng")

    _, actions = onboard(directory, employee("E1", "Grace", "Hopper", title="Engineering Manager"),
                         CONFIG, GROUPS)

    assert [(a["target"], a["result"]) for a in actions[1:]] == [
        ("all-staff", "done"), ("dept-eng", "failed"), ("managers", "done"),
    ]
    assert "Authorization_RequestDenied" in actions[2]["detail"]


def test_a_manager_who_does_not_exist_fails_that_step_only():
    directory = FakeDirectory()

    actions = run_joiners(directory, [employee("E2", "Ada", "Byron", manager_id="E404"),
                                      employee("E3", "Alan", "Turing")], CONFIG, GROUPS)

    failed = [a for a in actions if a["result"] == "failed"]
    assert [(a["employee_id"], a["action"], a["target"]) for a in failed] == [("E2", "set_manager", "E404")]
    assert len(directory.users) == 2                       # both accounts still created
    assert directory.groups_of("E2") == ["all-staff", "dept-eng"]


def test_a_dry_run_also_flags_a_manager_who_does_not_exist():
    actions = run_joiners(FakeDirectory(), [employee("E2", "Ada", "Byron", manager_id="E404")],
                          CONFIG, GROUPS, dry_run=True)

    assert [(a["action"], a["result"]) for a in actions][-1] == ("set_manager", "failed")


def test_a_manager_already_in_the_directory_need_not_be_in_the_file():
    directory = FakeDirectory()
    run_joiners(directory, [employee("E1", "Grace", "Hopper")], CONFIG, GROUPS)

    run_joiners(directory, [employee("E2", "Ada", "Byron", manager_id="E1")], CONFIG, GROUPS)

    assert directory.managers == {directory.user("E2")["id"]: directory.user("E1")["id"]}


def test_an_employee_id_on_two_accounts_is_refused_and_the_rest_continue():
    directory = FakeDirectory()
    for upn in ("one@d.com", "two@d.com"):
        directory.create_user({"userPrincipalName": upn, "employeeId": "E2"})
    before = len(directory.writes)

    actions = run_joiners(directory, [employee("E2", "Ada", "Byron"), employee("E3", "Alan", "Turing")],
                          CONFIG, GROUPS)

    assert actions[0]["result"] == "failed" and "on 2 accounts" in actions[0]["detail"]
    assert [w[0] for w in directory.writes[before:]] == ["create_user", "add_group_member", "add_group_member"]


def test_a_group_missing_from_the_tenant_is_a_failure_when_applying_and_a_note_in_a_dry_run():
    without_eng = {k: v for k, v in GROUPS.items() if k != "dept-eng"}
    ada = employee("E2", "Ada", "Byron")

    _, dry = onboard(FakeDirectory(), ada, CONFIG, without_eng, dry_run=True)
    _, real = onboard(FakeDirectory(), ada, CONFIG, without_eng)

    assert [(a["target"], a["result"]) for a in dry if a["detail"]] == [("dept-eng", "would")]
    assert [(a["target"], a["result"]) for a in real if a["detail"]] == [("dept-eng", "failed")]


def test_terminated_rows_are_not_onboarded():
    directory = FakeDirectory()

    actions = run_joiners(directory, [employee("E2", "Ada", "Byron", status="terminated")], CONFIG, GROUPS)

    assert actions == [] and directory.users == {}


# --- resolving group names -------------------------------------------------------

def test_groups_are_resolved_by_name_and_missing_ones_are_listed():
    directory = FakeDirectory(groups={"all-staff": "g-all", "dept-eng": "g-eng"})

    ids, missing = resolve_groups(directory, CONFIG)

    assert ids == {"all-staff": "g-all", "dept-eng": "g-eng"}
    assert missing == ["dept-sales", "managers"]


def test_two_groups_with_the_same_name_stop_the_run():
    import pytest

    from shared.rules import ProvisionError

    class Twins(FakeDirectory):
        def find_groups_by_name(self, name):
            return [{"id": "a"}, {"id": "b"}] if name == "all-staff" else super().find_groups_by_name(name)

    with pytest.raises(ProvisionError, match="2 groups are named 'all-staff'"):
        resolve_groups(Twins(), CONFIG)
