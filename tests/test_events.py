"""Tests for the two halves of the pipeline: an HR export becomes events
(plan_feed), and each event is carried out (handle_event).

The in-memory directory comes from test_onboard.py. No network.

Run from the repo root:  python -m pytest -q
"""

import pytest
from test_onboard import CONFIG, GROUPS, FakeDirectory, employee

from pathlib import Path

from shared.config import build_config
from shared.events import EventFailed, UnsupportedEvent, brake_reason, handle_event, plan_feed
from shared.hr_feed import FeedError

SAMPLES = Path(__file__).resolve().parents[1] / "sample-data"

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


def test_a_move_event_reaches_the_mover():
    directory = FakeDirectory()
    handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)

    actions = handle_event(
        directory,
        {"type": "move", "employee": employee("E2", "Ada", "Byron", department="Sales", title="Account Executive")},
        CONFIG, GROUPS,
    )

    assert directory.groups_of("E2") == ["all-staff", "dept-sales"]
    assert directory.user("E2")["department"] == "Sales"
    assert "failed" not in {a["result"] for a in actions}


def test_a_leave_event_reaches_the_leaver():
    directory = FakeDirectory()
    handle_event(directory, join("E2", "Ada", "Byron"), CONFIG, GROUPS)

    handle_event(directory, {"type": "leave", "employee": employee("E2", "Ada", "Byron"), "reason": "x"},
                 CONFIG, GROUPS)

    assert directory.user("E2")["accountEnabled"] is False
    assert directory.groups_of("E2") == []


def test_a_move_for_someone_with_no_account_fails_so_it_is_retried():
    with pytest.raises(EventFailed, match="no account carries this employeeId"):
        handle_event(FakeDirectory(), {"type": "move", "employee": employee("E2", "Ada", "Byron")},
                     CONFIG, GROUPS)


def test_an_unknown_event_type_is_refused():
    with pytest.raises(UnsupportedEvent, match="unknown event type"):
        handle_event(FakeDirectory(), {"type": "promote", "employee": {}}, CONFIG, GROUPS)


def test_a_dry_run_carries_out_nothing():
    directory = FakeDirectory()

    joined = handle_event(directory, join("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS, dry_run=True)
    managed = handle_event(directory, manager("E2", "Ada", "Byron", manager_id="E1"), CONFIG, GROUPS, dry_run=True)

    assert directory.writes == []
    assert {a["result"] for a in joined + managed} == {"would"}


# --- the mass-change brake --------------------------------------------------------

def roster(count):
    return HEADER + "".join(f"E{n},Ada,Byron,Engineering,Engineer,,,\n" for n in range(1, count + 1))


def test_an_export_that_would_offboard_too_many_people_is_refused():
    known = plan_feed(roster(20), CONFIG, None).snapshot

    plan = plan_feed(roster(10), CONFIG, known)        # half the company gone

    assert brake_reason(plan, known, 10) == "10 of 20 people would be offboarded; the limit is 10% (2)"


def test_leavers_within_the_limit_pass():
    known = plan_feed(roster(20), CONFIG, None).snapshot

    assert brake_reason(plan_feed(roster(18), CONFIG, known), known, 10) is None   # 2 of 20
    assert brake_reason(plan_feed(roster(17), CONFIG, known), known, 10) is not None  # 3 of 20


def test_one_leaver_is_always_allowed_however_small_the_company():
    known = plan_feed(roster(3), CONFIG, None).snapshot

    assert brake_reason(plan_feed(roster(2), CONFIG, known), known, 10) is None
    assert brake_reason(plan_feed(roster(1), CONFIG, known), known, 10) is not None


def test_the_limit_is_configurable():
    known = plan_feed(roster(20), CONFIG, None).snapshot
    plan = plan_feed(roster(10), CONFIG, known)

    assert brake_reason(plan, known, 50) is None


def test_joins_and_moves_never_trip_the_brake():
    known = plan_feed(roster(2), CONFIG, None).snapshot

    assert brake_reason(plan_feed(roster(40), CONFIG, known), known, 10) is None
    assert brake_reason(plan_feed(roster(40), CONFIG, None), None, 10) is None       # first export


def test_an_empty_file_trips_the_brake_instead_of_offboarding_everyone():
    known = plan_feed(roster(20), CONFIG, None).snapshot

    plan = plan_feed(HEADER, CONFIG, known)

    assert len([e for e in plan.events if e["type"] == "leave"]) == 20
    assert brake_reason(plan, known, 10) is not None


# --- the sample files shipped in the repo --------------------------------------------

def sample(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_day_two_after_day_one_is_two_joins_four_moves_and_two_leaves():
    config = build_config("d.com")
    day1 = plan_feed(sample("hr_export_day1.csv"), config, None)

    day2 = plan_feed(sample("hr_export_day2.csv"), config, day1.snapshot)

    assert day2.errors == []
    assert sorted((e["type"], e["employee"]["employee_id"]) for e in day2.events) == [
        ("join", "E1021"), ("join", "E1022"),
        ("leave", "E1012"), ("leave", "E1020"),
        ("move", "E1003"), ("move", "E1006"), ("move", "E1013"), ("move", "E1014"),
    ]
    changed = {e["employee"]["employee_id"]: e["changed"] for e in day2.events if e["type"] == "move"}
    assert changed == {
        "E1003": ["title"],
        "E1006": ["department", "title", "manager_id"],
        "E1013": ["title", "manager_id"],
        "E1014": ["manager_id"],
    }
    reasons = {e["employee"]["employee_id"]: e["reason"] for e in day2.events if e["type"] == "leave"}
    assert reasons == {"E1012": "status is terminated", "E1020": "no longer in the feed"}
    assert brake_reason(day2, day1.snapshot, 10) is None


def test_each_sample_file_processed_twice_plans_nothing_the_second_time():
    config = build_config("d.com")
    day1 = plan_feed(sample("hr_export_day1.csv"), config, None)
    assert plan_feed(sample("hr_export_day1.csv"), config, day1.snapshot).events == []

    day2 = plan_feed(sample("hr_export_day2.csv"), config, day1.snapshot)
    assert plan_feed(sample("hr_export_day2.csv"), config, day2.snapshot).events == []


def test_both_sample_days_run_end_to_end_and_a_rerun_changes_nothing():
    """The plan's idempotency test, against the in-memory directory: process
    day one, then day two, then day two again."""
    config = build_config("d.com")
    names = ["jml-all-staff", "jml-dept-engineering", "jml-dept-sales", "jml-dept-finance",
             "jml-dept-people", "jml-dept-it", "jml-people-managers"]
    groups = {name: f"g-{i}" for i, name in enumerate(names)}
    directory = FakeDirectory(groups=groups)

    def run(plan):
        results = []
        for event in plan.events + plan.manager_events:
            results += handle_event(directory, event, config, groups)
        return results

    day1 = plan_feed(sample("hr_export_day1.csv"), config, None)
    run(day1)
    day2 = plan_feed(sample("hr_export_day2.csv"), config, day1.snapshot)
    run(day2)
    writes = list(directory.writes)

    # Deliver every day-two message a second time, as a queue may.
    again = run(day2)

    assert directory.writes == writes
    assert {a["result"] for a in again} == {"skipped"}
    # And the directory is where day two says it should be.
    assert directory.user("E1012")["accountEnabled"] is False
    assert directory.user("E1020")["accountEnabled"] is False
    assert directory.groups_of("E1012") == [] and directory.groups_of("E1020") == []
    assert directory.groups_of("E1006") == ["jml-all-staff", "jml-dept-it"]
    assert directory.groups_of("E1013") == ["jml-all-staff", "jml-dept-finance", "jml-people-managers"]
    assert directory.user("E1003")["jobTitle"] == "Senior Software Engineer"
    assert directory.managers[directory.user("E1014")["id"]] == directory.user("E1013")["id"]
    assert len(directory.users) == 22
