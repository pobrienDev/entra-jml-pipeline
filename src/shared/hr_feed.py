"""Reads an HR export (CSV) into employee records.

One bad row must not stop everyone else's account from being processed, so
rows are validated one at a time: a row with a problem is reported and left
out, and the rest go through. Only a file that can't be read as an HR export
at all (missing columns) is rejected whole.
"""

import csv
import io
from datetime import date

from shared.rules import known_departments

COLUMNS = (
    "employee_id", "first_name", "last_name", "department",
    "title", "manager_id", "start_date", "status",
)
REQUIRED = ("employee_id", "first_name", "last_name", "department")
STATUSES = ("active", "terminated")


class FeedError(Exception):
    """The file as a whole is not a usable HR export."""


class RowError(str):
    """A rejected row's message, which also remembers whose row it was.

    The diff needs that: a row rejected for a typo must not make its
    employee look like they left the company.
    """

    def __new__(cls, message, employee_id=""):
        error = super().__new__(cls, message)
        error.employee_id = employee_id
        return error


def parse_hr_export(text, config=None):
    """Parse CSV text into (employees, errors).

    employees is a list of dicts keyed by COLUMNS, every value a stripped
    string (empty when absent; status defaults to "active"). errors is a
    list of RowError messages, one per rejected row, each naming the line
    and the employee. With a config, a department the department map doesn't know
    is an error: there would be no groups to give that person.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [(name or "").strip().lower() for name in (reader.fieldnames or [])]
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        raise FeedError(f"not an HR export: missing column(s) {', '.join(missing)}")
    departments = known_departments(config) if config else None

    employees, errors, seen = [], [], set()
    for line, raw in enumerate(reader, start=2):   # line 1 is the header
        row = {
            (key or "").strip().lower(): (value or "").strip()
            for key, value in raw.items() if key is not None
        }
        if not any(row.values()):
            continue   # blank line
        employee = {name: row.get(name, "") for name in COLUMNS}
        employee["status"] = employee["status"].lower() or "active"
        who = f"line {line} ({employee['employee_id'] or 'no employee_id'})"

        problems = [f"missing {name}" for name in REQUIRED if not employee[name]]
        if employee["employee_id"] and employee["employee_id"] in seen:
            problems.append("duplicate employee_id")
        if employee["status"] not in STATUSES:
            problems.append(f"status must be one of {', '.join(STATUSES)}")
        if employee["start_date"]:
            try:
                date.fromisoformat(employee["start_date"])
            except ValueError:
                problems.append("start_date must be YYYY-MM-DD")
        if employee["manager_id"] and employee["manager_id"] == employee["employee_id"]:
            problems.append("manager_id is the employee's own ID")
        if (departments is not None and employee["department"]
                and employee["department"].lower() not in departments):
            problems.append(f"department {employee['department']!r} is not in the department map")

        if problems:
            errors.append(RowError(f"{who}: {'; '.join(problems)}", employee["employee_id"]))
            continue
        seen.add(employee["employee_id"])
        employees.append(employee)
    return employees, errors
