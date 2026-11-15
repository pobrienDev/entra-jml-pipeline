"""The leaver: lock an account out, then clean up after it.

The steps and their order come from cmd_terminate in
employee-provisioning-tool: disable first, revoke sessions second, and only
then tidy up, so someone who has left is locked out even if a later step
fails. What changed in bringing it here:

- No prompts, previews or printing. It is called from a queue, returns
  action records, and is safe to call again.
- It removes only the groups the pipeline manages. The CLI strips every
  group it can; this service must never undo a membership it didn't grant,
  so anything else is reported for a person to review.
- It stamps the leave date on the account (employeeLeaveDateTime), which is
  also how a repeat run knows the work is already done.
- Left behind: converting the mailbox to shared and the Exchange
  distribution-list commands, which need a signed-in person.

split_licenses and group_kind are copied unchanged.
"""

from datetime import datetime, timezone

from shared.graph_api import GraphError
from shared.onboard import DONE, FAILED, SKIPPED, USER_FIELDS, WOULD, find_account, record

# A step the pipeline can't or mustn't do itself, left for a person.
MANUAL = "manual"

LEAVER_FIELDS = USER_FIELDS + ",assignedLicenses,licenseAssignmentStates,employeeLeaveDateTime"


def split_licenses(user):
    """(directly assigned skuIds, group-inherited skuIds) for an account.

    assignedLicenses lists inherited (group-based) licenses alongside direct
    ones without saying which is which, and an inherited license can't be
    removed from the user — the whole assignLicense call fails. The
    licenseAssignmentStates property tells them apart: assignedByGroup is
    null for a direct assignment. Without that property every license
    counts as direct.
    """
    assigned = [lic["skuId"] for lic in user.get("assignedLicenses") or []]
    states = user.get("licenseAssignmentStates")
    if states is None:
        return assigned, []
    direct = {
        state.get("skuId") for state in states
        if state.get("skuId") and not state.get("assignedByGroup")
    }
    return (
        [sku for sku in assigned if sku in direct],
        [sku for sku in assigned if sku not in direct],
    )


def group_kind(group):
    """How a membership can be ended: "graph" (normal group, removable via
    Graph), "exchange" (distribution list or mail-enabled security group —
    Exchange-only), or "dynamic" (membership follows attributes; nothing
    to remove by hand)."""
    types = group.get("groupTypes") or []
    if "DynamicMembership" in types:
        return "dynamic"
    if group.get("mailEnabled") and "Unified" not in types:
        return "exchange"
    return "graph"


def offboard(client, employee, config, group_ids, dry_run=False, now=None):
    """Offboard the account carrying this employee's ID. Returns actions.

    Lock out first, then clean up, recording each write as it lands, so a
    failed step can't hide the disable that already happened. The leave
    date is stamped last and only if everything before it worked: a retry
    after a partial failure therefore runs the whole sequence again, and
    each step skips what is already done.
    """
    account = find_account(client, employee["employee_id"], LEAVER_FIELDS)
    if account is None:
        return [record(employee, "disable_account", employee["employee_id"], SKIPPED,
                       "no account carries this employeeId")]
    user_id = account["id"]
    upn = account.get("userPrincipalName", user_id)
    stamped = bool(account.get("employeeLeaveDateTime"))
    actions = []

    def attempt(action, target, call, detail=""):
        """Record one write: would in a dry run, done or failed otherwise."""
        if dry_run:
            actions.append(record(employee, action, target, WOULD, detail))
            return True
        try:
            call()
        except GraphError as exc:
            actions.append(record(employee, action, target, FAILED, str(exc)))
            return False
        actions.append(record(employee, action, target, DONE, detail))
        return True

    # 1. Lock out.
    if account.get("accountEnabled") is False:
        actions.append(record(employee, "disable_account", upn, SKIPPED, "already disabled"))
    elif not attempt("disable_account", upn, lambda: client.update_user(user_id, {"accountEnabled": False})):
        return actions   # still signed in: nothing after this is worth doing yet
    if stamped and account.get("accountEnabled") is False:
        actions.append(record(employee, "revoke_sessions", upn, SKIPPED, "already offboarded"))
    else:
        attempt("revoke_sessions", upn, lambda: client.revoke_sessions(user_id))

    # 2. Leave the groups the pipeline manages. Only those.
    managed = {group_id: name for name, group_id in group_ids.items()}
    unmanaged = []
    for group in client.get_member_groups(user_id):
        name = managed.get(group["id"])
        if name is None:
            unmanaged.append(group.get("displayName") or group["id"])
        elif group_kind(group) != "graph":
            actions.append(record(employee, "remove_from_group", name, MANUAL,
                                  "not a group Microsoft Graph can change membership of"))
        else:
            attempt("remove_from_group", name,
                    lambda gid=group["id"]: client.remove_group_member(gid, user_id))
    if unmanaged:
        actions.append(record(
            employee, "review_memberships", f"{len(unmanaged)} group(s)", MANUAL,
            "not managed by the pipeline, left in place: " + ", ".join(sorted(unmanaged)),
        ))
    for role in client.get_member_roles(user_id):
        # Roles outlive a disable: a re-enabled account would hold them
        # again. Removing them needs a permission the pipeline deliberately
        # lacks, so they are handed to a person.
        actions.append(record(
            employee, "review_role", role.get("displayName") or role["id"], MANUAL,
            "directory role stays assigned — remove it in the Entra admin center",
        ))

    # 3. Free the licenses assigned directly to the account.
    licenses, inherited = split_licenses(account)
    if licenses:
        attempt("remove_licenses", f"{len(licenses)} license(s)",
                lambda: client.remove_licenses(user_id, licenses))
    if inherited:
        actions.append(record(
            employee, "remove_licenses", f"{len(inherited)} group-assigned license(s)", SKIPPED,
            "they follow the group membership(s) that grant them",
        ))

    # 4. Stamp the leave date: the marker a later clean-up job reads, and
    #    the sign to a repeat run that this account is finished.
    if stamped:
        actions.append(record(employee, "stamp_leave_date", upn, SKIPPED, "already stamped"))
    elif not any(a["result"] == FAILED for a in actions):
        when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        attempt("stamp_leave_date", upn,
                lambda: client.update_user(user_id, {"employeeLeaveDateTime": when}), when)
    return actions
