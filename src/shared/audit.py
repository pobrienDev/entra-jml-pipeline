"""The audit log: one row in Table Storage for every action the pipeline
takes, or in a dry run would have taken.

The CLI this replaces appended lines to a local log file. A service has no
local disk worth trusting, so the same record goes to a table instead: who
it was about, what was done, to what, how it went, and which HR export
caused it. Passwords and personal contact details never go in.

Rows are partitioned by UTC date, so "what did the pipeline do on the 15th"
is one cheap query. Entra ID's own audit log remains the authoritative
record of what changed in the directory; this one adds the why.
"""

import uuid
from datetime import datetime, timezone

from azure.data.tables import TableClient


def audit_entity(action, source, event_type, dry_run, attempt, now=None):
    """The table row for one action record."""
    now = now or datetime.now(timezone.utc)
    return {
        "PartitionKey": now.strftime("%Y-%m-%d"),
        # Time first so rows read in order; a random tail keeps two actions
        # in the same microsecond from colliding.
        "RowKey": f"{now.strftime('%H%M%S%f')}-{uuid.uuid4().hex[:8]}",
        "employee_id": action["employee_id"],
        "action": action["action"],
        "target": action["target"],
        "result": action["result"],
        "detail": action["detail"][:2000],
        "event_type": event_type,
        "source": source,
        "dry_run": bool(dry_run),
        "attempt": int(attempt),
    }


class AuditLog:
    def __init__(self, account, credential, table_name):
        self._table = TableClient(
            f"https://{account}.table.core.windows.net", table_name, credential=credential,
        )

    def write(self, actions, source="", event_type="", dry_run=False, attempt=1):
        """Record each action. An action that can't be recorded raises: a
        change the audit log doesn't know about is worse than a retry."""
        for action in actions:
            self._table.create_entity(audit_entity(action, source, event_type, dry_run, attempt))
