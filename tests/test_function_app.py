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


@pytest.fixture
def wired(monkeypatch):
    """The Function app with fakes in place of storage, Graph and settings."""
    storage, directory = FakeStorage({"day1.csv": DAY_ONE}), FakeDirectory()
    monkeypatch.setattr(function_app, "_state", {
        "storage": storage, "graph": directory, "config": CONFIG,
        "groups": (float("inf"), GROUPS),
    })
    monkeypatch.delenv("JML_DRY_RUN", raising=False)
    return storage, directory


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


def test_a_move_message_is_refused_rather_than_silently_dropped(wired):
    with pytest.raises(UnsupportedEvent):
        processor(message({"type": "move", "employee": {"employee_id": "E2"}}))


def test_the_dry_run_setting_stops_the_processor_writing(wired, monkeypatch):
    _, directory = wired
    monkeypatch.setenv("JML_DRY_RUN", "true")
    join = {"type": "join", "employee": {
        "employee_id": "E2", "first_name": "Ada", "last_name": "Byron",
        "department": "Engineering", "title": "", "manager_id": "", "start_date": "", "status": "active",
    }}

    processor(message(join))

    assert directory.writes == []
