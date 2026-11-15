"""The mover: bring an existing account in line with a changed HR record.

New for the pipeline; employee-provisioning-tool has no equivalent. When
someone changes department, title or manager, three things follow: their
attributes, their groups and who they report to.

Like the joiner, it reads before every write and acts only on the
difference, so it can be run again safely, and it compares the account with
the HR record rather than trusting the event to say what changed. A move
that was half applied is finished by running it again.

On groups it holds to one rule: it removes a membership only if the group
is one the department map names. Anything else on the account was granted
by someone else and stays.
"""

from shared.graph_api import GraphError
from shared.onboard import (
    DONE,
    FAILED,
    SKIPPED,
    USER_FIELDS,
    WOULD,
    assign_manager,
    find_account,
    record,
)
from shared.rules import groups_for

MOVER_FIELDS = USER_FIELDS + ",department,jobTitle"

# HR column -> the directory attribute it sets.
ATTRIBUTES = (("department", "department"), ("title", "jobTitle"))


def move(client, employee, config, group_ids, dry_run=False):
    """Apply a changed HR record to the employee's account. Returns actions.

    A failed step is recorded and the remaining steps are still attempted.
    """
    account = find_account(client, employee["employee_id"], MOVER_FIELDS)
    if account is None:
        return [record(employee, "update_attributes", employee["employee_id"], FAILED,
                       "no account carries this employeeId")]
    user_id = account["id"]
    upn = account.get("userPrincipalName", user_id)
    actions = []

    def attempt(action, target, call, detail=""):
        if dry_run:
            actions.append(record(employee, action, target, WOULD, detail))
            return
        try:
            call()
        except GraphError as exc:
            actions.append(record(employee, action, target, FAILED, str(exc)))
            return
        actions.append(record(employee, action, target, DONE, detail))

    # 1. Attributes.
    changes = {
        attribute: employee.get(column) or None
        for column, attribute in ATTRIBUTES
        if (account.get(attribute) or "") != (employee.get(column) or "")
    }
    if changes:
        summary = "; ".join(f"{name}: {account.get(name) or '(none)'} -> {value or '(none)'}"
                            for name, value in changes.items())
        attempt("update_attributes", upn, lambda: client.update_user(user_id, changes), summary)
    else:
        actions.append(record(employee, "update_attributes", upn, SKIPPED, "already up to date"))

    # 2. Groups: add what the new role needs before removing what it doesn't,
    #    so the person is never left without access in between.
    current = {group["id"] for group in client.get_member_groups(user_id)}
    wanted = set()
    for name in groups_for(employee, config):
        group_id = group_ids.get(name)
        if group_id is None:
            actions.append(record(employee, "add_to_group", name,
                                  WOULD if dry_run else FAILED, "group does not exist in the tenant"))
            continue
        wanted.add(group_id)
        if group_id not in current:
            attempt("add_to_group", name, lambda gid=group_id: client.add_group_member(gid, user_id))
    for name, group_id in group_ids.items():
        if group_id in current and group_id not in wanted:
            attempt("remove_from_group", name,
                    lambda gid=group_id: client.remove_group_member(gid, user_id))

    # 3. Manager.
    if employee.get("manager_id"):
        manager = find_account(client, employee["manager_id"])
        actions += assign_manager(
            client, employee, user_id, manager["id"] if manager else None, dry_run,
            manager_pending=dry_run,
        )
    elif client.get_manager(user_id):
        attempt("remove_manager", upn, lambda: client.remove_manager(user_id))
    return actions
