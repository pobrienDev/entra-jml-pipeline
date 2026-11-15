"""The two halves of the pipeline, without the Azure plumbing.

plan_feed is what the parser function does with an HR export: turn it into
events. handle_event is what the processor function does with one event.
function_app.py wraps them in triggers; everything worth testing is here.
"""

from collections import namedtuple

from shared.diff import compute_changes
from shared.hr_feed import parse_hr_export
from shared.onboard import FAILED, WOULD, assign_manager, find_account, onboard, record

FeedPlan = namedtuple("FeedPlan", "events manager_events snapshot errors")


class EventFailed(Exception):
    """An event could not be completed. Raising it makes the queue retry
    the message and, after the last attempt, move it to the poison queue.
    Every step checks before it acts, so a retry repeats nothing."""


class UnsupportedEvent(Exception):
    """An event type the processor has no handler for yet."""


def plan_feed(text, config, previous):
    """Turn an HR export into events.

    Returns a FeedPlan:
      events          join / move / leave, one per person who changed
      manager_events  one per joiner who has a manager, to be sent with a
                      delay so the manager's account exists first
      snapshot        what to remember for the next export
      errors          rejected rows
    Raises FeedError if the file isn't an HR export at all.
    """
    employees, errors = parse_hr_export(text, config)
    rejected = {error.employee_id for error in errors if error.employee_id}
    events, snapshot = compute_changes(previous, employees, rejected)
    manager_events = [
        {"type": "manager", "employee": event["employee"]}
        for event in events
        if event["type"] == "join" and event["employee"].get("manager_id")
    ]
    return FeedPlan(events, manager_events, snapshot, errors)


def handle_event(client, event, config, group_ids, dry_run=False):
    """Carry out one event. Returns the action records.

    Raises EventFailed if any step failed, with the records attached as
    .actions, and UnsupportedEvent for a type with no handler.
    """
    kind = event.get("type")
    employee = event.get("employee") or {}

    if kind == "join":
        _, actions = onboard(client, employee, config, group_ids, dry_run)
    elif kind == "manager":
        actions = _set_manager(client, employee, dry_run)
    elif kind in ("move", "leave"):
        raise UnsupportedEvent(f"{kind} events are not handled yet ({employee.get('employee_id')})")
    else:
        raise UnsupportedEvent(f"unknown event type {kind!r}")

    failed = [a for a in actions if a["result"] == FAILED]
    if failed:
        error = EventFailed("; ".join(
            f"{a['action']} {a['target']}: {a['detail']}" for a in failed
        ))
        error.actions = actions
        raise error
    return actions


def _set_manager(client, employee, dry_run):
    account = find_account(client, employee["employee_id"])
    if account is None:
        if dry_run:   # the join was a dry run too, so there is no account
            return [record(employee, "set_manager", employee.get("manager_id", ""), WOULD)]
        return [record(employee, "set_manager", employee.get("manager_id", ""), FAILED,
                       "the employee's own account does not exist yet")]
    manager = find_account(client, employee["manager_id"]) if employee.get("manager_id") else None
    return assign_manager(
        client, employee, account["id"], manager["id"] if manager else None, dry_run,
        # In a dry run the manager's join was a dry run too, so a missing
        # manager account is expected rather than an error.
        manager_pending=dry_run,
    )
