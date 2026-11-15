"""The joiner: make sure an employee has an account, groups and a manager.

Every step reads the directory first and acts only on the difference, so
running it again for the same person changes nothing. People are recognised
by employeeId, never by name.

Each function returns a list of action records rather than printing or
logging, so the caller decides where they go: the console for a local run,
the audit table once the pipeline runs in Azure. In a dry run every read
still happens, so the records are accurate; every write is replaced by a
record whose result is "would".
"""

from shared.graph_api import GraphError
from shared.rules import (
    ProvisionError,
    display_name_for,
    groups_for,
    managed_groups,
    pick_upn,
    temp_password,
)

USER_FIELDS = "id,displayName,userPrincipalName,accountEnabled,employeeId"

# Results an action record can carry.
DONE, SKIPPED, WOULD, FAILED = "done", "skipped", "would", "failed"


def record(employee, action, target, result, detail=""):
    """One audit-ready line: who, what, to what, how it went."""
    return {
        "employee_id": employee["employee_id"],
        "action": action,
        "target": target,
        "result": result,
        "detail": detail,
    }


def resolve_groups(client, config):
    """Look up the object ID of every group the department map names.

    Returns (ids, missing): ids maps group name -> object ID, missing lists
    names with no such group. Two groups sharing a name is an error: the
    pipeline would have to guess which one to change.
    """
    ids, missing = {}, []
    for name in managed_groups(config):
        matches = client.find_groups_by_name(name)
        if len(matches) > 1:
            raise ProvisionError(
                f"{len(matches)} groups are named {name!r} — group names in the "
                "department map must be unique in the tenant"
            )
        if matches:
            ids[name] = matches[0]["id"]
        else:
            missing.append(name)
    return ids, missing


def find_account(client, employee_id):
    """The account carrying this employeeId, or None."""
    matches = client.find_users_by_employee_id(employee_id, USER_FIELDS)
    if len(matches) > 1:
        upns = ", ".join(sorted(m.get("userPrincipalName", "?") for m in matches))
        raise ProvisionError(
            f"employeeId {employee_id} is on {len(matches)} accounts ({upns}) — "
            "resolve the duplicate before the pipeline touches either"
        )
    return matches[0] if matches else None


class _WithClaimedUpns:
    """The client as pick_upn sees it, plus the UPNs already handed out in
    this run.

    Two people in one file can want the same UPN (Jordan Lee and Jamie Lee).
    The directory can't be relied on to show the first one as taken when the
    second is checked: in a dry run it was never created, and after a real
    create a new account can take a few seconds to become readable. So UPNs
    picked earlier in the run are remembered here and count as taken.
    """

    def __init__(self, client, claimed):
        self._client = client
        self._claimed = claimed

    def get_user(self, upn, select):
        if upn.lower() in self._claimed:
            return {"id": None, "displayName": "picked earlier in this run"}
        return self._client.get_user(upn, select)

    def address_holder(self, local, domain):
        return self._client.address_holder(local, domain)


def new_user_payload(employee, upn, config):
    """The body for creating the account. The temporary password is
    generated here and goes nowhere else: it is not returned, logged or
    recorded."""
    payload = {
        "accountEnabled": True,
        "displayName": display_name_for(employee),
        "givenName": employee["first_name"],
        "surname": employee["last_name"],
        "userPrincipalName": upn,
        "mailNickname": upn.split("@", 1)[0],
        "usageLocation": (config.get("tenant") or {}).get("usage_location", "US"),
        "employeeId": employee["employee_id"],
        "department": employee["department"],
        "passwordProfile": {
            "password": temp_password(),
            "forceChangePasswordNextSignIn": True,
        },
    }
    if employee.get("title"):
        payload["jobTitle"] = employee["title"]
    if employee.get("start_date"):
        payload["employeeHireDate"] = f"{employee['start_date']}T00:00:00Z"
    return payload


def onboard(client, employee, config, group_ids, dry_run=False, claimed_upns=None):
    """Create the employee's account if they have none, then add them to any
    of their groups they aren't in yet.

    claimed_upns is a set shared across one run: UPNs picked for earlier
    employees, which this one must not be given (see _WithClaimedUpns).

    Returns (user_id, actions). user_id is None only in a dry run for a
    person who has no account yet. A group step that fails is recorded and
    the remaining groups are still attempted.
    """
    actions = []
    claimed_upns = claimed_upns if claimed_upns is not None else set()
    account = find_account(client, employee["employee_id"])

    if account:
        user_id = account["id"]
        actions.append(record(
            employee, "create_user", account.get("userPrincipalName", user_id),
            SKIPPED, "account already exists",
        ))
        current = {group["id"] for group in client.get_member_groups(user_id)}
    else:
        upn = pick_upn(_WithClaimedUpns(client, claimed_upns), employee, config)
        claimed_upns.add(upn.lower())
        current = set()
        if dry_run:
            user_id = None
            actions.append(record(employee, "create_user", upn, WOULD))
        else:
            try:
                created = client.create_user(new_user_payload(employee, upn, config))
            except GraphError as exc:
                # A duplicate UPN is reported as Request_BadRequest, no more
                # specific code, so the message text is matched here. The
                # pre-check can miss an account created seconds ago.
                if exc.status == 400 and "userPrincipalName already exists" in str(exc):
                    raise ProvisionError(
                        f"{upn} was taken between the check and the create — run again"
                    ) from exc
                raise
            user_id = created["id"]
            actions.append(record(employee, "create_user", created.get("userPrincipalName", upn), DONE))

    for name in groups_for(employee, config):
        group_id = group_ids.get(name)
        if group_id is None:
            actions.append(record(
                employee, "add_to_group", name,
                WOULD if dry_run else FAILED, "group does not exist in the tenant",
            ))
        elif group_id in current:
            actions.append(record(employee, "add_to_group", name, SKIPPED, "already a member"))
        elif dry_run:
            actions.append(record(employee, "add_to_group", name, WOULD))
        else:
            try:
                client.add_group_member(group_id, user_id)
                actions.append(record(employee, "add_to_group", name, DONE))
            except GraphError as exc:
                # "already a member" comes back as Request_BadRequest, the same
                # code as a malformed request, so the message text has to do.
                if exc.status == 400 and "already exist" in str(exc):
                    actions.append(record(employee, "add_to_group", name, SKIPPED, "already a member"))
                else:
                    actions.append(record(employee, "add_to_group", name, FAILED, str(exc)))
    return user_id, actions


def assign_manager(client, employee, user_id, manager_user_id, dry_run=False,
                   manager_pending=False):
    """Point the employee's account at their manager's, if it doesn't already.

    user_id and manager_user_id are directory object IDs. In a dry run
    either can be None for an account this run would create; manager_pending
    says that is why the manager has no ID, as opposed to there being no
    such person. Returns a list of actions (empty for an employee with no
    manager_id).
    """
    manager_ref = employee.get("manager_id")
    if not manager_ref:
        return []
    if manager_user_id is None:
        if dry_run and manager_pending:
            return [record(employee, "set_manager", manager_ref, WOULD)]
        return [record(employee, "set_manager", manager_ref, FAILED,
                       "no account found for the manager's employeeId")]
    if user_id is None:   # dry run, the employee's own account not created yet
        return [record(employee, "set_manager", manager_ref, WOULD)]

    current = client.get_manager(user_id)
    if current and current.get("id") == manager_user_id:
        return [record(employee, "set_manager", manager_ref, SKIPPED, "already set")]
    if dry_run:
        return [record(employee, "set_manager", manager_ref, WOULD)]
    try:
        client.set_manager(user_id, manager_user_id)
    except GraphError as exc:
        return [record(employee, "set_manager", manager_ref, FAILED, str(exc))]
    return [record(employee, "set_manager", manager_ref, DONE)]


def run_joiners(client, employees, config, group_ids, dry_run=False):
    """Onboard every active employee, then set managers.

    Two passes, because a manager's account has to exist before anyone can
    report to it and the feed is in no particular order. One person's failure
    is recorded against them and the rest continue. Returns all actions.
    """
    actions, user_ids, claimed_upns = [], {}, set()
    active = [e for e in employees if e.get("status", "active") == "active"]

    for employee in active:
        try:
            user_id, done = onboard(client, employee, config, group_ids, dry_run, claimed_upns)
        except (GraphError, ProvisionError) as exc:
            actions.append(record(employee, "create_user", employee["employee_id"], FAILED, str(exc)))
            continue
        actions += done
        user_ids[employee["employee_id"]] = user_id

    for employee in active:
        if employee["employee_id"] not in user_ids or not employee.get("manager_id"):
            continue
        manager_ref = employee["manager_id"]
        try:
            if manager_ref in user_ids:
                manager_user_id = user_ids[manager_ref]
            else:   # not in this file: perhaps already in the directory
                manager = find_account(client, manager_ref)
                manager_user_id = manager["id"] if manager else None
            actions += assign_manager(
                client, employee, user_ids[employee["employee_id"]], manager_user_id, dry_run,
                manager_pending=manager_ref in user_ids,
            )
        except (GraphError, ProvisionError) as exc:
            actions.append(record(employee, "set_manager", manager_ref, FAILED, str(exc)))
    return actions
