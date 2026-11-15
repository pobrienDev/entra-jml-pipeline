"""Tests for snapshot diffing: who joined, who moved, who left.

Pure functions, no storage and no network.

Run from the repo root:  python -m pytest -q
"""

from shared.diff import compute_changes


def person(employee_id, department="Engineering", title="Engineer", manager_id="E1", status="active", **extra):
    return {"employee_id": employee_id, "first_name": "Ada", "last_name": "Byron",
            "department": department, "title": title, "manager_id": manager_id,
            "start_date": "", "status": status, **extra}


def kinds(events):
    return [(event["type"], event["employee"]["employee_id"]) for event in events]


def test_the_first_export_is_all_joins():
    events, snapshot = compute_changes(None, [person("E1", manager_id=""), person("E2")])

    assert kinds(events) == [("join", "E1"), ("join", "E2")]
    assert set(snapshot) == {"E1", "E2"}


def test_the_same_export_again_produces_no_events():
    people = [person("E1", manager_id=""), person("E2")]
    _, snapshot = compute_changes(None, people)

    events, again = compute_changes(snapshot, people)

    assert events == []
    assert again == snapshot


def test_a_new_person_in_a_later_export_is_the_only_join():
    _, snapshot = compute_changes(None, [person("E1")])

    events, _ = compute_changes(snapshot, [person("E1"), person("E2")])

    assert kinds(events) == [("join", "E2")]


def test_a_change_of_department_title_or_manager_is_a_move_naming_what_changed():
    _, snapshot = compute_changes(None, [person("E2")])

    events, after = compute_changes(snapshot, [person("E2", department="Sales", manager_id="E9")])

    (move,) = events
    assert move["type"] == "move"
    assert move["changed"] == ["department", "manager_id"]
    assert move["previous"]["department"] == "Engineering"
    assert move["employee"]["department"] == "Sales"
    assert after["E2"]["department"] == "Sales"


def test_a_change_the_pipeline_does_not_act_on_is_not_a_move():
    _, snapshot = compute_changes(None, [person("E2")])

    events, after = compute_changes(snapshot, [person("E2", start_date="2026-12-01")])

    assert events == []
    assert after["E2"]["start_date"] == "2026-12-01"   # still remembered


def test_someone_missing_from_the_export_has_left():
    _, snapshot = compute_changes(None, [person("E1"), person("E2")])

    events, after = compute_changes(snapshot, [person("E1")])

    assert kinds(events) == [("leave", "E2")]
    assert events[0]["reason"] == "no longer in the feed"
    assert set(after) == {"E1"}


def test_a_terminated_status_is_a_leave_and_drops_them_from_the_snapshot():
    _, snapshot = compute_changes(None, [person("E2")])

    events, after = compute_changes(snapshot, [person("E2", status="terminated")])

    assert kinds(events) == [("leave", "E2")]
    assert events[0]["reason"] == "status is terminated"
    assert after == {}


def test_a_leave_carries_the_record_as_it_was_last_known():
    _, snapshot = compute_changes(None, [person("E2", department="Sales")])

    events, _ = compute_changes(snapshot, [])

    assert events[0]["employee"]["department"] == "Sales"


def test_someone_terminated_who_was_never_onboarded_produces_nothing():
    events, snapshot = compute_changes(None, [person("E2", status="terminated")])

    assert events == [] and snapshot == {}


def test_a_leaver_is_only_reported_once():
    _, snapshot = compute_changes(None, [person("E2")])
    _, snapshot = compute_changes(snapshot, [])

    events, _ = compute_changes(snapshot, [])

    assert events == []


def test_a_rejected_row_does_not_look_like_a_departure():
    _, snapshot = compute_changes(None, [person("E1"), person("E2")])

    # E2's row had a typo and was rejected by validation, so it isn't in
    # `employees` at all. They must not be offboarded for it.
    events, after = compute_changes(snapshot, [person("E1")], rejected_ids={"E2"})

    assert events == []
    assert after["E2"] == snapshot["E2"]   # carried forward as last known


def test_a_rejected_row_for_someone_new_just_waits_for_a_corrected_export():
    events, snapshot = compute_changes(None, [person("E1")], rejected_ids={"E2"})

    assert kinds(events) == [("join", "E1")]
    assert "E2" not in snapshot


def test_someone_who_left_and_comes_back_is_a_join_again():
    _, snapshot = compute_changes(None, [person("E2")])
    _, snapshot = compute_changes(snapshot, [])

    events, _ = compute_changes(snapshot, [person("E2")])

    assert kinds(events) == [("join", "E2")]


def test_joins_moves_and_leaves_in_one_export():
    _, snapshot = compute_changes(None, [person("E1"), person("E2"), person("E3")])

    events, _ = compute_changes(snapshot, [person("E1"), person("E2", title="Senior Engineer"), person("E4")])

    assert kinds(events) == [("move", "E2"), ("join", "E4"), ("leave", "E3")]
