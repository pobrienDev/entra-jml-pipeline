"""Tests for reading an HR export: good rows go through, a bad row is reported
and left out without taking the rest of the file with it.

Run from the repo root:  python -m pytest -q
"""

from pathlib import Path

import pytest

from shared.config import build_config, load_department_map
from shared.hr_feed import FeedError, parse_hr_export
from shared.rules import ProvisionError

HEADER = "employee_id,first_name,last_name,department,title,manager_id,start_date,status\n"
CONFIG = {"groups": {"all_employees": [], "departments": {"Engineering": ["g"], "Sales": ["g"]}, "titles": {}}}
REPO = Path(__file__).resolve().parents[1]


def parse(rows, config=None):
    return parse_hr_export(HEADER + rows, config)


def test_a_clean_row_becomes_an_employee_record():
    employees, errors = parse("E1, Ada ,Byron,Engineering,Engineer,E9,2026-11-02,active\n")

    assert errors == []
    assert employees == [{
        "employee_id": "E1", "first_name": "Ada", "last_name": "Byron",
        "department": "Engineering", "title": "Engineer", "manager_id": "E9",
        "start_date": "2026-11-02", "status": "active",
    }]


def test_optional_columns_may_be_empty_and_status_defaults_to_active():
    employees, errors = parse("E1,Ada,Byron,Engineering,,,,\n")

    assert errors == []
    assert employees[0]["status"] == "active"
    assert employees[0]["manager_id"] == "" and employees[0]["title"] == ""


def test_a_bad_row_is_reported_and_the_rest_still_go_through():
    employees, errors = parse(
        "E1,Ada,Byron,Engineering,,,,\n"
        "E2,,Hopper,Engineering,,,,\n"
        "E3,Alan,Turing,Engineering,,,,\n"
    )

    assert [e["employee_id"] for e in employees] == ["E1", "E3"]
    assert errors == ["line 3 (E2): missing first_name"]


def test_a_repeated_employee_id_keeps_the_first_row_only():
    employees, errors = parse(
        "E1,Ada,Byron,Engineering,,,,\n"
        "E1,Ada,Lovelace,Engineering,,,,\n"
    )

    assert [e["last_name"] for e in employees] == ["Byron"]
    assert errors == ["line 3 (E1): duplicate employee_id"]


@pytest.mark.parametrize("row, problem", [
    ("E1,Ada,Byron,Engineering,,,11/02/2026,\n", "start_date must be YYYY-MM-DD"),
    ("E1,Ada,Byron,Engineering,,,,on leave\n", "status must be one of active, terminated"),
    ("E1,Ada,Byron,Engineering,,E1,,\n", "manager_id is the employee's own ID"),
    (",Ada,Byron,Engineering,,,,\n", "missing employee_id"),
])
def test_each_kind_of_bad_value_is_named(row, problem):
    employees, errors = parse(row)

    assert employees == []
    assert len(errors) == 1 and problem in errors[0]


def test_one_row_can_have_several_problems():
    _, errors = parse("E1,,,Engineering,,,soon,\n")

    assert errors == ["line 2 (E1): missing first_name; missing last_name; start_date must be YYYY-MM-DD"]


def test_a_department_the_map_does_not_know_is_rejected_when_a_config_is_given():
    employees, errors = parse(
        "E1,Ada,Byron,engineering,,,,\n"     # case differs: still known
        "E2,Grace,Hopper,Legal,,,,\n",
        CONFIG,
    )

    assert [e["employee_id"] for e in employees] == ["E1"]
    assert errors == ["line 3 (E2): department 'Legal' is not in the department map"]


def test_blank_lines_and_a_byte_order_mark_are_ignored():
    employees, errors = parse_hr_export("﻿" + HEADER + "\nE1,Ada,Byron,Engineering,,,,\n,,,,,,,\n")

    assert errors == [] and len(employees) == 1


def test_column_order_and_header_case_do_not_matter():
    text = "Last_Name,First_Name,Department,Employee_ID\nByron,Ada,Engineering,E1\n"

    employees, errors = parse_hr_export(text)

    assert errors == []
    assert employees[0]["employee_id"] == "E1" and employees[0]["first_name"] == "Ada"


def test_a_file_without_the_required_columns_is_rejected_whole():
    with pytest.raises(FeedError, match="missing column"):
        parse_hr_export("name,team\nAda,Engineering\n")


def test_terminated_rows_are_parsed_not_dropped():
    employees, _ = parse("E1,Ada,Byron,Engineering,,,,Terminated\n")

    assert employees[0]["status"] == "terminated"


# --- the files shipped in the repo ---------------------------------------------

def test_the_day_one_sample_parses_cleanly_against_the_real_department_map():
    config = build_config("d.com")
    text = (REPO / "sample-data" / "hr_export_day1.csv").read_text(encoding="utf-8")

    employees, errors = parse_hr_export(text, config)

    assert errors == []
    assert len(employees) == 20
    ids = {e["employee_id"] for e in employees}
    assert all(e["manager_id"] in ids for e in employees if e["manager_id"])
    assert len({e["department"] for e in employees}) == 5


def test_every_group_in_the_real_department_map_carries_the_project_prefix():
    department_map = load_department_map()
    names = list(department_map["all_employees"])
    for section in ("departments", "titles"):
        for groups in department_map[section].values():
            names += groups

    assert names and all(name.startswith("jml-") for name in names)


# --- department map loading ------------------------------------------------------

@pytest.mark.parametrize("content, problem", [
    ("not json", "not valid JSON"),
    ("[]", "must be a JSON object"),
    ('{"departments": {"Sales": "jml-sales"}}', "departments.Sales must be a list"),
    ('{"departments": []}', "departments must be an object"),
    ('{"all_employees": ["jml-all"]}', "no departments defined"),
])
def test_a_malformed_department_map_stops_the_run(tmp_path, content, problem):
    path = tmp_path / "map.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ProvisionError, match=problem):
        load_department_map(path)


def test_a_missing_department_map_is_a_clear_error(tmp_path):
    with pytest.raises(ProvisionError, match="cannot read the department map"):
        load_department_map(tmp_path / "nope.json")
