"""Tests for the two halves of the pipeline: an HR export becomes events
(plan_feed), and each event is carried out (handle_event).

The in-memory directory comes from test_onboard.py. No network.

Run from the repo root:  python -m pytest -q
"""

import pytest
from test_onboard import CONFIG, GROUPS, FakeDirectory, employee

from shared.events import EventFailed, UnsupportedEvent, handle_event, plan_feed
from shared.hr_feed import FeedError

HEADER = "employee_id,first_name,last_name,department,title,manager_id,start_date,status\n"
DAY_ONE = HEADER + (
    "E1,Grace,Hopper,Engineering,Engineering Manager,,,\n"
    "E2,Ada,Byron,Engineering,Software Engineer,E1,,\n"
    "E3,Alan,Turing,Sales,Account Executive,E1,,\n"
)


def join(*args, **kwargs):
    return {"type": "join", "employee": employee(*args, **kwargs)}


def manager(*args, **kwargs):
    return {"type": "manager", "employee": employee(*args, **kwargs)}


# --- an export becomes events ---------------------------------------------------

def test_a_first_export_plans_a_join_each_and_a_manager_message_per_report():
    plan = plan_feed(DAY_ONE, CONFIG, None)

    assert [(e["type"], e["employee"]["employee_id"]) for e in plan.events] == [
        ("join", "E1"), ("join", "E2"), ("join", "E3"),
    ]
    assert [e["employee"]["employee_id"] for e in plan.manager_events] == ["E2", "E3"]
    assert all(e["type"] == "manager" for e in plan.manager_events)
    assert set(plan.snapshot) == {"E1", "E2", "E3"}
    assert plan.errors == []


def test_the_same_export_again_plans_nothing():
    first = plan_feed(DAY_ONE, CONFIG, None)

    again = plan_feed(DAY_ONE, CONFIG, first.snapshot)

    assert again.events == [] and again.manager_events == []
    assert again.snapshot == first.snapshot


def test_a_row_broken_in_a_later_export_is_reported_and_its_person_left_alone():
    first = plan_feed(DAY_ONE, CONFIG, None)
    broken = DAY_ONE.replace("E3,Alan,Turing,Sales", "E3,,Turing,Sales")

    plan = plan_feed(broken, CONFIG, first.snapshot)

    assert plan.events == []                                   # in particular, no leave for E3
    assert plan.errors == ["line 4 (E3): missing first_name"]
    assert plan.snapshot["E3"] == first.snapshot["E3"]


def test_a_file_that_is_not_an_hr_export_plans_nothing_and_says_so():
    with pytest.raises(FeedError):
        plan_feed("name,team\nAda,Engineering\n", CONFIG, None)


def test_events_survive_a_round_trip_through_json():
    import json

    plan = plan_feed(DAY_ONE, CONFIG, None)

    assert json.loads(json.dumps(plan.events + plan.manager_events)) == plan.events + plan.manager_events


# --- an event is carried out ----------------------------------------------------

def test_a_join_event_creates_the_account_and_memberships():
    directory = FakeDirectory()

    actions = handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)

    assert [a["result"] for a in actions] == ["done", "done", "done"]
    assert directory.groups_of("E2") == ["all-staff", "dept-eng"]


def test_a_join_event_delivered_twice_changes_nothing_the_second_time():
    directory = FakeDirectory()
    handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)
    writes = list(directory.writes)

    actions = handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)

    assert directory.writes == writes
    assert {a["result"] for a in actions} == {"skipped"}


def test_a_manager_event_sets_the_manager_once_both_accounts_exist():
    directory = FakeDirectory()
    handle_event(directory, join("E1", "Grace", "Hopper"), CONFIG, GROUPS)
    handle_event(directory, join("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS)

    actions = handle_event(directory, manager("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS)

    assert [(a["action"], a["result"]) for a in actions] == [("set_manager", "done")]
    assert directory.managers == {directory.user("E2")["id"]: directory.user("E1")["id"]}


def test_a_manager_event_that_arrives_too_early_fails_so_the_queue_retries_it():
    directory = FakeDirectory()
    handle_event(directory, join("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS)

    with pytest.raises(EventFailed, match="no account found for the manager"):
        handle_event(directory, manager("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS)

    # The manager's join lands, the retry succeeds.
    handle_event(directory, join("E1", "Grace", "Hopper"), CONFIG, GROUPS)
    actions = handle_event(directory, manager("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS)
    assert actions[0]["result"] == "done"


def test_a_manager_event_before_the_employees_own_account_exists_also_fails():
    with pytest.raises(EventFailed, match="own account does not exist yet"):
        handle_event(FakeDirectory(), manager("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS)


def test_a_failed_step_fails_the_event_and_attaches_what_did_happen():
    directory = FakeDirectory(fail_group="g-eng")

    with pytest.raises(EventFailed, match="add_to_group dept-eng") as excinfo:
        handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)

    assert [a["result"] for a in excinfo.value.actions] == ["done", "done", "failed"]


def test_a_retry_after_a_partial_failure_only_does_what_is_left():
    directory = FakeDirectory(fail_group="g-eng")
    with pytest.raises(EventFailed):
        handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)
    directory.fail_group = None        # whatever was wrong is fixed
    before = len(directory.writes)

    actions = handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)

    assert [w[0] for w in directory.writes[before:]] == ["add_group_member"]
    assert [a["result"] for a in actions] == ["skipped", "skipped", "done"]


def test_a_group_that_does_not_exist_fails_the_event():
    without_eng = {k: v for k, v in GROUPS.items() if k != "dept-eng"}

    with pytest.raises(EventFailed, match="group does not exist"):
        handle_event(FakeDirectory(), join("E2", "Ada", "Byron"), CONFIG, without_eng)


@pytest.mark.parametrize("kind", ["move", "leave"])
def test_moves_and_leaves_are_refused_until_they_are_built(kind):
    directory = FakeDirectory()

    with pytest.raises(UnsupportedEvent, match="not handled yet"):
        handle_event(directory, {"type": kind, "employee": employee("E2", "Ada", "Byron")}, CONFIG, GROUPS)

    assert directory.writes == []


def test_an_unknown_event_type_is_refused():
    with pytest.raises(UnsupportedEvent, match="unknown event type"):
        handle_event(FakeDirectory(), {"type": "promote", "employee": {}}, CONFIG, GROUPS)


def test_a_dry_run_carries_out_nothing():
    directory = FakeDirectory()

    joined = handle_event(directory, join("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS, dry_run=True)
    managed = handle_event(directory, manager("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS, dry_run=True)

    assert directory.writes == []
    assert {a["result"] for a in joined + managed} == {"would"}
