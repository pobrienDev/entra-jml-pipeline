"""Works out what changed between two HR exports.

The pipeline keeps a snapshot of the last export it processed. Comparing a
new export with it gives the events to act on: who joined, who moved, who
left. A pure function: no storage, no network.
"""

# A change to any of these makes a "move". Anything else on the record (a
# corrected start date, say) is not something the pipeline acts on.
TRACKED = ("department", "title", "manager_id")


def compute_changes(previous, employees, rejected_ids=()):
    """Compare the previous snapshot with a new export.

    previous       {employee_id: record} from the last export, or None/empty
                   the first time
    employees      the new export's valid rows (from parse_hr_export)
    rejected_ids   employee IDs whose rows were rejected as invalid

    Returns (events, snapshot). events is a list of dicts:

      {"type": "join",  "employee": record}
      {"type": "move",  "employee": record, "previous": record, "changed": [fields]}
      {"type": "leave", "employee": previous record, "reason": text}

    snapshot is what to remember for next time: everyone active now.

    Someone whose row was rejected is carried forward unchanged. A typo in
    one row must not read as that person having left, so they produce no
    event and stay in the snapshot as they were.

    Processing the same export twice yields no events the second time.
    """
    previous = previous or {}
    rejected = set(rejected_ids)
    current = {employee["employee_id"]: employee for employee in employees}
    events, snapshot = [], {}

    for employee_id, employee in current.items():
        before = previous.get(employee_id)
        if employee.get("status", "active") != "active":
            if before:
                events.append({"type": "leave", "employee": before, "reason": "status is terminated"})
            continue   # terminated and never onboarded: nothing to undo
        snapshot[employee_id] = employee
        if before is None:
            events.append({"type": "join", "employee": employee})
            continue
        changed = [f for f in TRACKED if (before.get(f) or "") != (employee.get(f) or "")]
        if changed:
            events.append({"type": "move", "employee": employee, "previous": before, "changed": changed})

    for employee_id, before in previous.items():
        if employee_id in current:
            continue
        if employee_id in rejected:
            snapshot[employee_id] = before
            continue
        events.append({"type": "leave", "employee": before, "reason": "no longer in the feed"})

    return events, snapshot
