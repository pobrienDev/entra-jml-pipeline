"""Tests for the trigger wrappers in function_app.py: what the parser reads
and queues for an upload, and that a processor failure reaches the queue.

No Azure: storage and Microsoft Graph are replaced by in-memory fakes, and
the functions are called directly with hand-built trigger payloads.

Run from the repo root:  python -m pytest -q
"""

import json

import azure.functions as func
import pytest
from test_onboard import CONFIG, GROUPS, FakeDirectory

import function_app
from shared.events import EventFailed, UnsupportedEvent

ACCOUNT = "stexample"
FEED = "https://stexample.blob.core.windows.net/hr-feed/"
HEADER = "employee_id,first_name,last_name,department,title,manager_id,start_date,status\n"
DAY_ONE = HEADER + (
    "E1,Grace,Hopper,Engineering,Engineering Manager,,,\n"
    "E2,Ada,Byron,Engineering,Software Engineer,E1,,\n"
)


class FakeStorage:
    """In-memory stand-in for shared.storage.PipelineStorage."""

    account = ACCOUNT
    feed_container = "hr-feed"

    def __init__(self, blobs=None, snapshot=None):
        self.blobs = dict(blobs or {})
        self.snapshot = snapshot
        self.sent = []          # (event, delay_seconds)
        self.saved = []         # (employees, source, etag)
        self.fail_send_after = None

    def read_feed(self, blob_name):
        return self.blobs[blob_name]

    def load_snapshot(self):
        return (self.snapshot, "etag-1") if self.snapshot is not None else (None, None)

    def save_snapshot(self, employees, source, etag):
        self.saved.append((employees, source, etag))
        self.snapshot = employees

    def send(self, event, delay_seconds=0):
        if self.fail_send_after is not None and len(self.sent) >= self.fail_send_after:
            raise RuntimeError("queue unavailable")
        self.sent.append((event, delay_seconds))


class FakeAudit:
    """In-memory stand-in for shared.audit.AuditLog."""

    def __init__(self):
        self.rows = []

    def write(self, actions, source="", event_type="", dry_run=False, attempt=1):
        for action in actions:
            self.rows.append({**action, "source": source, "event_type": event_type,
                              "dry_run": dry_run, "attempt": attempt})


@pytest.fixture
def wired(monkeypatch):
    """The Function app with fakes in place of storage, Graph and settings."""
    storage, directory = FakeStorage({"day1.csv": DAY_ONE}), FakeDirectory()
    monkeypatch.setattr(function_app, "_state", {
        "storage": storage, "graph": directory, "config": CONFIG,
        "groups": (float("inf"), GROUPS), "audit": FakeAudit(),
    })
    monkeypatch.delenv("JML_DRY_RUN", raising=False)
    monkeypatch.delenv("JML_MAX_LEAVE_PERCENT", raising=False)
    return storage, directory


def audit_rows():
    return function_app._state["audit"].rows


parser = function_app.parser.build().get_user_function()
processor = function_app.processor.build().get_user_function()


def upload(url):
    return func.EventGridEvent(
        id="1", data={"url": url, "api": "PutBlob"}, topic="t", subject="s",
        event_type="Microsoft.Storage.BlobCreated", event_time=None, data_version="1",
    )


class FakeQueueMessage:
    """The two things the processor reads from a queue message."""

    def __init__(self, body, dequeue_count):
        self._body = body
        self.dequeue_count = dequeue_count

    def get_body(self):
        return self._body


def message(event, dequeue_count=1):
    return FakeQueueMessage(json.dumps(event).encode(), dequeue_count)


# --- parser ---------------------------------------------------------------------

def test_an_upload_queues_joins_now_and_manager_messages_for_later(wired):
    storage, _ = wired

    parser(upload(FEED + "day1.csv"))

    assert [(e["type"], e["employee"]["employee_id"], delay) for e, delay in storage.sent] == [
        ("join", "E1", 0), ("join", "E2", 0), ("manager", "E2", function_app.MANAGER_DELAY_SECONDS),
    ]
    assert all(e["source"] == "day1.csv" for e, _ in storage.sent)
    assert function_app.MANAGER_DELAY_SECONDS > 0


def test_the_snapshot_is_saved_after_the_messages_with_the_etag_it_was_loaded_with(wired):
    storage, _ = wired

    parser(upload(FEED + "day1.csv"))

    ((employees, source, etag),) = storage.saved
    assert set(employees) == {"E1", "E2"} and source == "day1.csv"
    assert etag is None            # there was no snapshot before the first export


def test_uploading_the_same_file_again_queues_nothing(wired):
    storage, _ = wired
    parser(upload(FEED + "day1.csv"))
    sent = len(storage.sent)

    parser(upload(FEED + "day1.csv"))

    assert len(storage.sent) == sent
    assert storage.saved[-1][2] == "etag-1"     # written against the snapshot it read


def test_a_failure_while_queueing_leaves_the_snapshot_alone_so_a_redelivery_starts_over(wired):
    storage, _ = wired
    storage.fail_send_after = 1

    with pytest.raises(RuntimeError):
        parser(upload(FEED + "day1.csv"))

    assert storage.saved == [] and storage.snapshot is None


def test_a_file_that_is_not_an_hr_export_is_reported_not_retried(wired, caplog):
    storage, _ = wired
    storage.blobs["notes.csv"] = "name,team\nAda,Engineering\n"

    parser(upload(FEED + "notes.csv"))        # returns normally: no redelivery

    assert storage.sent == [] and storage.saved == []
    assert "rejected" in caplog.text


@pytest.mark.parametrize("url", [
    "https://elsewhere.blob.core.windows.net/hr-feed/day1.csv",     # another storage account
    "https://stexample.blob.core.windows.net/jml-state/day1.csv",   # another container
    "https://stexample.blob.core.windows.net/hr-feed/readme.txt",   # not a CSV
    "https://stexample.blob.core.windows.net.evil.example/hr-feed/day1.csv",
    "",
])
def test_the_parser_only_reads_csvs_from_its_own_feed_container(wired, url):
    storage, _ = wired

    parser(upload(url))

    assert storage.sent == [] and storage.saved == []


def test_a_csv_in_a_folder_inside_the_feed_container_is_read(wired):
    storage, _ = wired
    storage.blobs["2026/day 1.csv"] = DAY_ONE

    parser(upload(FEED + "2026/day%201.csv"))

    assert len(storage.sent) == 3


# --- processor ------------------------------------------------------------------

def test_a_join_message_creates_the_account(wired):
    _, directory = wired
    storage = FakeStorage({"day1.csv": DAY_ONE})
    function_app._state["storage"] = storage
    parser(upload(FEED + "day1.csv"))

    for event, _ in storage.sent:
        processor(message(event))

    assert directory.groups_of("E2") == ["all-staff", "dept-eng"]
    assert directory.managers == {directory.user("E2")["id"]: directory.user("E1")["id"]}


def test_a_failed_event_raises_so_the_queue_retries_and_then_poisons_it(wired):
    _, directory = wired
    directory.fail_group = "g-eng"
    join = {"type": "join", "employee": {
        "employee_id": "E2", "first_name": "Ada", "last_name": "Byron",
        "department": "Engineering", "title": "", "manager_id": "", "start_date": "", "status": "active",
    }}

    with pytest.raises(EventFailed):
        processor(message(join, dequeue_count=3))


def test_a_message_of_an_unknown_type_is_refused_rather_than_silently_dropped(wired):
    with pytest.raises(UnsupportedEvent):
        processor(message({"type": "promote", "employee": {"employee_id": "E2"}}))


# --- the audit log -----------------------------------------------------------------

JOIN_ADA = {"type": "join", "source": "day1.csv", "employee": {
    "employee_id": "E2", "first_name": "Ada", "last_name": "Byron",
    "department": "Engineering", "title": "", "manager_id": "", "start_date": "", "status": "active",
}}


def test_every_action_is_written_to_the_audit_log_with_its_export_and_attempt(wired):
    processor(message(JOIN_ADA, dequeue_count=2))

    assert [(r["action"], r["result"]) for r in audit_rows()] == [
        ("create_user", "done"), ("add_to_group", "done"), ("add_to_group", "done"),
    ]
    assert {(r["source"], r["event_type"], r["attempt"], r["dry_run"]) for r in audit_rows()} == {
        ("day1.csv", "join", 2, False),
    }


def test_a_dry_run_is_audited_as_what_would_have_happened(wired, monkeypatch):
    monkeypatch.setenv("JML_DRY_RUN", "true")

    processor(message(JOIN_ADA))

    assert {(r["result"], r["dry_run"]) for r in audit_rows()} == {("would", True)}


def test_a_failed_event_still_audits_what_did_happen_before_it_failed(wired):
    _, directory = wired
    directory.fail_group = "g-eng"

    with pytest.raises(EventFailed):
        processor(message(JOIN_ADA))

    assert [(r["action"], r["result"]) for r in audit_rows()] == [
        ("create_user", "done"), ("add_to_group", "done"), ("add_to_group", "failed"),
    ]


def test_the_final_failed_attempt_is_marked_for_the_alert_and_audited(wired, caplog):
    _, directory = wired
    directory.fail_group = "g-eng"

    with pytest.raises(EventFailed):
        processor(message(JOIN_ADA, dequeue_count=function_app.MAX_ATTEMPTS))

    assert "poison: join E2 moved to the poison queue after 5 attempts" in caplog.text
    assert audit_rows()[-1]["action"] == "poison_message"


def test_an_earlier_failed_attempt_is_not_marked_as_poison(wired, caplog):
    _, directory = wired
    directory.fail_group = "g-eng"

    with pytest.raises(EventFailed):
        processor(message(JOIN_ADA, dequeue_count=function_app.MAX_ATTEMPTS - 1))

    assert "poison:" not in caplog.text
    assert "poison_message" not in {r["action"] for r in audit_rows()}


def test_max_attempts_matches_the_queue_setting_in_host_json():
    from pathlib import Path

    host = json.loads((Path(function_app.__file__).parent / "host.json").read_text())

    assert host["extensions"]["queues"]["maxDequeueCount"] == function_app.MAX_ATTEMPTS


# --- the mass-change brake -----------------------------------------------------------

def roster(count):
    return HEADER + "".join(f"E{n},Ada,Byron,Engineering,Engineer,,,\n" for n in range(1, count + 1))


def test_an_export_that_drops_half_the_company_is_halted_not_processed(wired, caplog):
    storage, _ = wired
    storage.blobs.update({"full.csv": roster(20), "half.csv": roster(10)})
    parser(upload(FEED + "full.csv"))
    sent, snapshot = len(storage.sent), dict(storage.snapshot)

    parser(upload(FEED + "half.csv"))

    assert len(storage.sent) == sent                 # nothing queued
    assert storage.snapshot == snapshot              # snapshot untouched: fix and re-upload
    assert "brake: half.csv not processed: 10 of 20 people would be offboarded" in caplog.text
    assert [(r["action"], r["result"]) for r in audit_rows()][-1] == ("mass_change_brake", "failed")


def test_after_the_brake_the_corrected_file_processes_normally(wired):
    storage, _ = wired
    storage.blobs.update({"full.csv": roster(20), "half.csv": roster(10), "fixed.csv": roster(19)})
    parser(upload(FEED + "full.csv"))
    parser(upload(FEED + "half.csv"))
    sent = len(storage.sent)

    parser(upload(FEED + "fixed.csv"))

    assert [(e["type"], e["employee"]["employee_id"]) for e, _ in storage.sent[sent:]] == [("leave", "E20")]


def test_the_brake_limit_comes_from_configuration(wired, monkeypatch):
    storage, _ = wired
    storage.blobs.update({"full.csv": roster(20), "half.csv": roster(10)})
    parser(upload(FEED + "full.csv"))
    sent = len(storage.sent)
    monkeypatch.setenv("JML_MAX_LEAVE_PERCENT", "60")

    parser(upload(FEED + "half.csv"))

    assert len(storage.sent) == sent + 10


def test_a_dry_run_does_not_use_the_changes_up(wired, monkeypatch):
    storage, directory = wired
    monkeypatch.setenv("JML_DRY_RUN", "true")

    parser(upload(FEED + "day1.csv"))
    for event, _ in list(storage.sent):
        processor(message(event))

    assert storage.saved == [] and directory.writes == []      # nothing remembered, nothing changed

    # Dry run switched off, same file uploaded again: the same events, now for real.
    monkeypatch.setenv("JML_DRY_RUN", "false")
    before = len(storage.sent)
    parser(upload(FEED + "day1.csv"))

    assert [e["type"] for e, _ in storage.sent[before:]] == ["join", "join", "manager"]
    assert len(storage.saved) == 1


def test_rejected_rows_and_an_unreadable_file_are_audited(wired):
    storage, _ = wired
    storage.blobs["typo.csv"] = DAY_ONE.replace("E2,Ada", "E2,")
    storage.blobs["notes.csv"] = "name,team\nAda,Engineering\n"

    parser(upload(FEED + "typo.csv"))
    parser(upload(FEED + "notes.csv"))

    assert [(r["action"], r["target"]) for r in audit_rows()] == [
        ("reject_row", "typo.csv"), ("read_export", "notes.csv"),
    ]


def test_the_dry_run_setting_stops_the_processor_writing(wired, monkeypatch):
    _, directory = wired
    monkeypatch.setenv("JML_DRY_RUN", "true")
    join = {"type": "join", "employee": {
        "employee_id": "E2", "first_name": "Ada", "last_name": "Byron",
        "department": "Engineering", "title": "", "manager_id": "", "start_date": "", "status": "active",
    }}

    processor(message(join))

    assert directory.writes == []
