"""Tests for the mover: attributes, groups and manager follow a changed HR
record, only managed groups are ever removed, and a second run changes
nothing.

The in-memory directory comes from test_onboard.py. No network.

Run from the repo root:  python -m pytest -q
"""

import pytest
from test_onboard import CONFIG, GROUPS, FakeDirectory, employee

from shared.mover import move
from shared.onboard import run_joiners

GRACE = employee("E1", "Grace", "Hopper", title="Engineering Manager")
VICTOR = employee("E8", "Victor", "Almeida", department="Sales", title="Sales Lead")
ADA = employee("E2", "Ada", "Byron", manager_id="E1")


@pytest.fixture
def staffed():
    directory = FakeDirectory(groups={**GROUPS, "someone-elses-group": "g-other"})
    run_joiners(directory, [GRACE, VICTOR, ADA], CONFIG, GROUPS)
    return directory


def steps(actions):
    return [(a["action"], a["target"], a["result"]) for a in actions]


def new_writes(directory, before):
    return [w[0] for w in directory.writes[before:]]


# --- a change of department -------------------------------------------------------

def test_a_department_change_updates_attributes_swaps_groups_and_sets_the_new_manager(staffed):
    to_sales = employee("E2", "Ada", "Byron", department="Sales", title="Account Executive", manager_id="E8")

    actions = move(staffed, to_sales, CONFIG, GROUPS)

    ada = staffed.user("E2")
    assert (ada["department"], ada["jobTitle"]) == ("Sales", "Account Executive")
    assert staffed.groups_of("E2") == ["all-staff", "dept-sales"]
    assert staffed.managers[ada["id"]] == staffed.user("E8")["id"]
    assert steps(actions) == [
        ("update_attributes", "abyron@d.com", "done"),
        ("add_to_group", "dept-sales", "done"),
        ("remove_from_group", "dept-eng", "done"),
        ("set_manager", "E8", "done"),
    ]


def test_the_new_group_is_added_before_the_old_one_is_removed(staffed):
    before = len(staffed.writes)

    move(staffed, employee("E2", "Ada", "Byron", department="Sales", manager_id="E1"), CONFIG, GROUPS)

    membership = [w for w in new_writes(staffed, before) if "group" in w]
    assert membership == ["add_group_member", "remove_group_member"]


def test_the_audit_detail_says_what_changed_from_and_to(staffed):
    actions = move(staffed, employee("E2", "Ada", "Byron", department="Sales", manager_id="E1"), CONFIG, GROUPS)

    assert actions[0]["detail"] == "department: Engineering -> Sales"


# --- a promotion ------------------------------------------------------------------

def test_a_title_that_maps_to_a_group_gains_it(staffed):
    promoted = employee("E2", "Ada", "Byron", title="Engineering Manager", manager_id="E1")

    move(staffed, promoted, CONFIG, GROUPS)

    assert staffed.groups_of("E2") == ["all-staff", "dept-eng", "managers"]


def test_losing_that_title_loses_the_group(staffed):
    stepped_down = employee("E1", "Grace", "Hopper", title="Principal Engineer")

    move(staffed, stepped_down, CONFIG, GROUPS)

    assert staffed.groups_of("E1") == ["all-staff", "dept-eng"]


def test_a_title_change_with_no_group_behind_it_touches_no_memberships(staffed):
    before = len(staffed.writes)

    move(staffed, employee("E2", "Ada", "Byron", title="Senior Software Engineer", manager_id="E1"),
         CONFIG, GROUPS)

    assert new_writes(staffed, before) == ["update_user"]


# --- only what the pipeline manages ------------------------------------------------

def test_a_membership_the_pipeline_did_not_grant_survives_a_move(staffed):
    staffed.members["g-other"].add(staffed.user("E2")["id"])

    move(staffed, employee("E2", "Ada", "Byron", department="Sales", manager_id="E1"), CONFIG, GROUPS)

    assert staffed.groups_of("E2") == ["all-staff", "dept-sales", "someone-elses-group"]


# --- manager ----------------------------------------------------------------------

def test_a_manager_change_alone_only_sets_the_manager(staffed):
    before = len(staffed.writes)

    actions = move(staffed, employee("E2", "Ada", "Byron", manager_id="E8"), CONFIG, GROUPS)

    assert new_writes(staffed, before) == ["set_manager"]
    assert steps(actions) == [
        ("update_attributes", "abyron@d.com", "skipped"), ("set_manager", "E8", "done"),
    ]


def test_a_manager_removed_in_hr_is_removed_from_the_account(staffed):
    actions = move(staffed, employee("E2", "Ada", "Byron", manager_id=""), CONFIG, GROUPS)

    assert staffed.user("E2")["id"] not in staffed.managers
    assert steps(actions)[-1] == ("remove_manager", "abyron@d.com", "done")


def test_a_new_manager_who_has_no_account_yet_fails_that_step_only(staffed):
    to_sales = employee("E2", "Ada", "Byron", department="Sales", manager_id="E404")

    actions = move(staffed, to_sales, CONFIG, GROUPS)

    assert steps(actions)[-1] == ("set_manager", "E404", "failed")
    assert staffed.groups_of("E2") == ["all-staff", "dept-sales"]      # the rest still happened


# --- idempotency ----------------------------------------------------------------

def test_a_second_run_changes_nothing(staffed):
    to_sales = employee("E2", "Ada", "Byron", department="Sales", title="Account Executive", manager_id="E8")
    move(staffed, to_sales, CONFIG, GROUPS)
    writes = list(staffed.writes)

    actions = move(staffed, to_sales, CONFIG, GROUPS)

    assert staffed.writes == writes
    assert steps(actions) == [
        ("update_attributes", "abyron@d.com", "skipped"), ("set_manager", "E8", "skipped"),
    ]


def test_a_move_for_someone_whose_account_already_matches_is_a_no_op(staffed):
    before = len(staffed.writes)

    move(staffed, ADA, CONFIG, GROUPS)

    assert new_writes(staffed, before) == []


def test_a_half_applied_move_is_finished_by_running_it_again(staffed):
    to_sales = employee("E2", "Ada", "Byron", department="Sales", manager_id="E1")
    staffed.fail_on = {"remove_group_member"}
    first = move(staffed, to_sales, CONFIG, GROUPS)
    staffed.fail_on = set()
    before = len(staffed.writes)

    second = move(staffed, to_sales, CONFIG, GROUPS)

    assert ("remove_from_group", "dept-eng", "failed") in steps(first)
    assert new_writes(staffed, before) == ["remove_group_member"]
    assert "failed" not in {a["result"] for a in second}
    assert staffed.groups_of("E2") == ["all-staff", "dept-sales"]


# --- failures and dry run ---------------------------------------------------------

def test_someone_with_no_account_cannot_be_moved():
    actions = move(FakeDirectory(), ADA, CONFIG, GROUPS)

    assert steps(actions) == [("update_attributes", "E2", "failed")]


def test_a_refused_attribute_update_is_recorded_and_the_groups_still_move(staffed):
    staffed.fail_on = {"update_user"}

    actions = move(staffed, employee("E2", "Ada", "Byron", department="Sales", manager_id="E1"), CONFIG, GROUPS)

    assert steps(actions)[0] == ("update_attributes", "abyron@d.com", "failed")
    assert staffed.groups_of("E2") == ["all-staff", "dept-sales"]


def test_a_dry_run_writes_nothing_and_lists_every_step(staffed):
    before = len(staffed.writes)
    to_sales = employee("E2", "Ada", "Byron", department="Sales", title="Account Executive", manager_id="E8")

    actions = move(staffed, to_sales, CONFIG, GROUPS, dry_run=True)

    assert new_writes(staffed, before) == []
    assert [(a["action"], a["result"]) for a in actions] == [
        ("update_attributes", "would"), ("add_to_group", "would"),
        ("remove_from_group", "would"), ("set_manager", "would"),
    ]
    assert staffed.user("E2")["department"] == "Engineering"


def test_a_group_missing_from_the_tenant_fails_that_step(staffed):
    without_sales = {k: v for k, v in GROUPS.items() if k != "dept-sales"}

    actions = move(staffed, employee("E2", "Ada", "Byron", department="Sales", manager_id="E1"),
                   CONFIG, without_sales)

    assert ("add_to_group", "dept-sales", "failed") in steps(actions)
