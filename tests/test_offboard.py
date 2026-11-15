"""Tests for the leaver: lock out first, clean up after, touch only what the
pipeline manages, and change nothing on a second run.

The in-memory directory comes from test_onboard.py. No network.

Run from the repo root:  python -m pytest -q
"""

from datetime import datetime, timezone

import pytest
from test_onboard import CONFIG, GROUPS, FakeDirectory, employee

from shared.offboard import group_kind, offboard, split_licenses
from shared.onboard import onboard

NOW = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
GRACE = employee("E1", "Grace", "Hopper", title="Engineering Manager")


@pytest.fixture
def staffed():
    """A directory where Grace has already been onboarded."""
    directory = FakeDirectory(groups={**GROUPS, "someone-elses-group": "g-other"})
    onboard(directory, GRACE, CONFIG, GROUPS)
    return directory


def steps(actions):
    return [(a["action"], a["result"]) for a in actions]


def new_writes(directory, before):
    return [w[0] for w in directory.writes[before:]]


# --- a first run ----------------------------------------------------------------

def test_offboarding_locks_out_first_then_cleans_up_then_stamps_the_date(staffed):
    before = len(staffed.writes)

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert new_writes(staffed, before) == [
        "update_user", "revoke_sessions",                       # locked out before anything else
        "remove_group_member", "remove_group_member", "remove_group_member",
        "update_user",                                          # the leave date, last
    ]
    assert steps(actions) == [
        ("disable_account", "done"), ("revoke_sessions", "done"),
        ("remove_from_group", "done"), ("remove_from_group", "done"), ("remove_from_group", "done"),
        ("stamp_leave_date", "done"),
    ]
    grace = staffed.user("E1")
    assert grace["accountEnabled"] is False
    assert grace["employeeLeaveDateTime"] == "2026-11-20T09:30:00Z"
    assert staffed.groups_of("E1") == []


def test_the_account_is_disabled_not_deleted(staffed):
    offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert len(staffed.users) == 1


# --- only what the pipeline manages ------------------------------------------------

def test_a_membership_the_pipeline_did_not_grant_is_left_and_flagged_for_review(staffed):
    staffed.members["g-other"].add(staffed.user("E1")["id"])

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert staffed.groups_of("E1") == ["someone-elses-group"]
    (review,) = [a for a in actions if a["action"] == "review_memberships"]
    assert review["result"] == "manual"
    assert "someone-elses-group" in review["detail"]


def test_a_directory_role_is_flagged_not_removed(staffed):
    staffed.roles[staffed.user("E1")["id"]] = [{"id": "r1", "displayName": "User Administrator"}]

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    (review,) = [a for a in actions if a["action"] == "review_role"]
    assert (review["target"], review["result"]) == ("User Administrator", "manual")


def test_a_follow_up_for_a_person_does_not_count_as_a_failure(staffed):
    staffed.roles[staffed.user("E1")["id"]] = [{"id": "r1", "displayName": "User Administrator"}]

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert "failed" not in {a["result"] for a in actions}
    assert staffed.user("E1")["employeeLeaveDateTime"]          # still finished and stamped


# --- idempotency ----------------------------------------------------------------

def test_a_second_run_changes_nothing(staffed):
    offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)
    writes = list(staffed.writes)

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert staffed.writes == writes
    assert steps(actions) == [
        ("disable_account", "skipped"), ("revoke_sessions", "skipped"), ("stamp_leave_date", "skipped"),
    ]


def test_the_original_leave_date_survives_a_second_run(staffed):
    offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    offboard(staffed, GRACE, CONFIG, GROUPS, now=datetime(2027, 1, 1, tzinfo=timezone.utc))

    assert staffed.user("E1")["employeeLeaveDateTime"] == "2026-11-20T09:30:00Z"


def test_someone_with_no_account_is_a_skip_not_an_error():
    actions = offboard(FakeDirectory(), GRACE, CONFIG, GROUPS, now=NOW)

    assert steps(actions) == [("disable_account", "skipped")]


# --- failures -------------------------------------------------------------------

def test_if_the_lockout_fails_nothing_else_is_attempted(staffed):
    staffed.fail_on = {"update_user"}
    before = len(staffed.writes)

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert steps(actions) == [("disable_account", "failed")]
    assert new_writes(staffed, before) == []


def test_a_failed_group_removal_is_recorded_the_rest_continue_and_the_date_is_not_stamped(staffed):
    staffed.fail_on = {"remove_group_member"}

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert staffed.user("E1")["accountEnabled"] is False        # the lockout still happened
    assert [a["result"] for a in actions if a["action"] == "remove_from_group"] == ["failed"] * 3
    assert "stamp_leave_date" not in {a["action"] for a in actions}
    assert "employeeLeaveDateTime" not in staffed.user("E1")


def test_a_retry_after_a_partial_failure_finishes_the_job(staffed):
    staffed.fail_on = {"remove_group_member"}
    offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)
    staffed.fail_on = set()
    before = len(staffed.writes)

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    # Not stamped yet, so sessions are revoked again (harmless) and the
    # clean-up that failed is done; the account is not disabled twice.
    assert new_writes(staffed, before) == [
        "revoke_sessions", "remove_group_member", "remove_group_member", "remove_group_member", "update_user",
    ]
    assert steps(actions)[0] == ("disable_account", "skipped")
    assert staffed.groups_of("E1") == [] and staffed.user("E1")["employeeLeaveDateTime"]


# --- dry run --------------------------------------------------------------------

def test_a_dry_run_writes_nothing_and_lists_every_step(staffed):
    before = len(staffed.writes)

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, dry_run=True, now=NOW)

    assert new_writes(staffed, before) == []
    assert steps(actions) == [
        ("disable_account", "would"), ("revoke_sessions", "would"),
        ("remove_from_group", "would"), ("remove_from_group", "would"), ("remove_from_group", "would"),
        ("stamp_leave_date", "would"),
    ]
    assert staffed.user("E1")["accountEnabled"] is True


# --- licenses -------------------------------------------------------------------

def test_directly_assigned_licenses_are_removed_and_group_assigned_ones_left(staffed):
    staffed.user("E1").update({
        "assignedLicenses": [{"skuId": "direct"}, {"skuId": "via-group"}],
        "licenseAssignmentStates": [
            {"skuId": "direct", "assignedByGroup": None},
            {"skuId": "via-group", "assignedByGroup": "g-lic"},
        ],
    })

    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert ("remove_licenses", staffed.user("E1")["id"], ("direct",)) in staffed.writes
    assert [(a["target"], a["result"]) for a in actions if a["action"] == "remove_licenses"] == [
        ("1 license(s)", "done"), ("1 group-assigned license(s)", "skipped"),
    ]


def test_no_licenses_means_no_license_step(staffed):
    actions = offboard(staffed, GRACE, CONFIG, GROUPS, now=NOW)

    assert "remove_licenses" not in {a["action"] for a in actions}


# --- the copied helpers -----------------------------------------------------------

def test_split_licenses_treats_everything_as_direct_without_assignment_states():
    assert split_licenses({"assignedLicenses": [{"skuId": "a"}, {"skuId": "b"}]}) == (["a", "b"], [])
    assert split_licenses({}) == ([], [])


@pytest.mark.parametrize("group, kind", [
    ({"groupTypes": [], "mailEnabled": False}, "graph"),
    ({"groupTypes": ["Unified"], "mailEnabled": True}, "graph"),
    ({"groupTypes": [], "mailEnabled": True}, "exchange"),
    ({"groupTypes": ["DynamicMembership"], "mailEnabled": False}, "dynamic"),
])
def test_group_kind_says_how_a_membership_can_be_ended(group, kind):
    assert group_kind(group) == kind


def test_a_managed_group_graph_cannot_change_is_flagged_not_attempted(staffed):
    class DynamicEngineering(FakeDirectory):
        def get_member_groups(self, user_id):
            groups = super().get_member_groups(user_id)
            for group in groups:
                if group["id"] == "g-eng":
                    group["groupTypes"] = ["DynamicMembership"]
            return groups

    directory = DynamicEngineering()
    onboard(directory, GRACE, CONFIG, GROUPS)

    actions = offboard(directory, GRACE, CONFIG, GROUPS, now=NOW)

    assert ("dept-eng", "manual") in [(a["target"], a["result"]) for a in actions]
    assert directory.groups_of("E1") == ["dept-eng"]
