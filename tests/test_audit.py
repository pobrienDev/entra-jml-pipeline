"""Tests for the shape of an audit row. The table itself is Azure's; what
matters here is what goes into it.

Run from the repo root:  python -m pytest -q
"""

from datetime import datetime, timezone

from shared.audit import audit_entity

NOW = datetime(2026, 11, 20, 9, 30, 15, 123456, tzinfo=timezone.utc)
ACTION = {"employee_id": "E2", "action": "add_to_group", "target": "jml-dept-sales",
          "result": "done", "detail": ""}


def test_a_row_says_who_what_to_what_how_it_went_and_why():
    row = audit_entity(ACTION, "hr_export_day2.csv", "move", dry_run=False, attempt=2, now=NOW)

    assert row["employee_id"] == "E2"
    assert (row["action"], row["target"], row["result"]) == ("add_to_group", "jml-dept-sales", "done")
    assert (row["event_type"], row["source"]) == ("move", "hr_export_day2.csv")
    assert row["dry_run"] is False and row["attempt"] == 2


def test_rows_are_partitioned_by_utc_date_and_sort_by_time_within_it():
    early = audit_entity(ACTION, "", "", False, 1, now=NOW)
    late = audit_entity(ACTION, "", "", False, 1, now=NOW.replace(hour=17))

    assert early["PartitionKey"] == late["PartitionKey"] == "2026-11-20"
    assert early["RowKey"].startswith("093015123456-")
    assert early["RowKey"] < late["RowKey"]


def test_two_actions_in_the_same_instant_get_different_keys():
    keys = {audit_entity(ACTION, "", "", False, 1, now=NOW)["RowKey"] for _ in range(100)}

    assert len(keys) == 100


def test_a_dry_run_is_marked_as_one():
    row = audit_entity({**ACTION, "result": "would"}, "", "join", dry_run=True, attempt=1, now=NOW)

    assert row["dry_run"] is True and row["result"] == "would"


def test_an_enormous_error_message_is_cut_to_fit_a_table_property():
    row = audit_entity({**ACTION, "detail": "x" * 50_000}, "", "", False, 1, now=NOW)

    assert len(row["detail"]) == 2000
