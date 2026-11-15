"""The Function app: two triggers around the logic in shared/.

  parser      An HR export lands in the feed container. Event Grid calls this
              function, which diffs the export against the last snapshot and
              queues one message per change.
  processor   Takes one message off the queue and carries it out against
              Microsoft Graph, as the managed identity.

Failures are left to the platform on purpose. An exception in the processor
makes the queue retry the message (five attempts, 30 seconds apart, see
host.json) and then move it to the poison queue, where it is visible and
nothing is lost. Every step checks before it acts, so a retry repeats
nothing.
"""

import json
import logging
import os
import time
from urllib.parse import unquote, urlparse

import azure.functions as func

from shared.auth import pipeline_credential
from shared.config import build_config
from shared.events import handle_event, plan_feed
from shared.graph_api import GraphClient
from shared.hr_feed import FeedError
from shared.onboard import resolve_groups
from shared.storage import PipelineStorage

app = func.FunctionApp()

# Managers are set by a second message, held back so the accounts it refers
# to have been created by then.
MANAGER_DELAY_SECONDS = int(os.environ.get("JML_MANAGER_DELAY_SECONDS", "90"))
GROUP_CACHE_SECONDS = 600

_state = {}


def _dry_run():
    return os.environ.get("JML_DRY_RUN", "false").strip().lower() in ("1", "true", "yes")


def _config():
    if "config" not in _state:
        _state["config"] = build_config(
            os.environ["JML_UPN_DOMAIN"], os.environ.get("JML_USAGE_LOCATION", "US"),
        )
    return _state["config"]


def _storage():
    if "storage" not in _state:
        _state["storage"] = PipelineStorage(
            os.environ["JML_STORAGE_ACCOUNT"], pipeline_credential(),
            os.environ["JML_FEED_CONTAINER"], os.environ["JML_STATE_CONTAINER"],
            os.environ["JML_QUEUE_NAME"],
        )
    return _state["storage"]


def _graph():
    if "graph" not in _state:
        _state["graph"] = GraphClient(pipeline_credential())
    return _state["graph"]


def _group_ids():
    """Group name -> object ID, looked up once and reused for ten minutes
    rather than on every message."""
    cached = _state.get("groups")
    if cached and time.time() - cached[0] < GROUP_CACHE_SECONDS:
        return cached[1]
    ids, missing = resolve_groups(_graph(), _config())
    if missing:
        logging.warning("groups in the department map that don't exist: %s", ", ".join(missing))
    _state["groups"] = (time.time(), ids)
    return ids


def _feed_blob_name(url):
    """The blob's name, if the URL is a CSV in this pipeline's own feed
    container; None for anything else. The function reads only what its
    configuration names, whatever an event points at."""
    storage = _storage()
    parsed = urlparse(url)
    if parsed.hostname != f"{storage.account}.blob.core.windows.net":
        return None
    container, _, name = unquote(parsed.path).lstrip("/").partition("/")
    if container != storage.feed_container or not name.lower().endswith(".csv"):
        return None
    return name


@app.function_name(name="parser")
@app.event_grid_trigger(arg_name="event")
def parser(event: func.EventGridEvent) -> None:
    url = (event.get_json() or {}).get("url", "")
    blob_name = _feed_blob_name(url)
    if blob_name is None:
        logging.warning("parser: ignoring an event that is not a CSV in the feed container")
        return

    storage = _storage()
    previous, etag = storage.load_snapshot()
    try:
        plan = plan_feed(storage.read_feed(blob_name), _config(), previous)
    except FeedError as exc:
        # Not an HR export at all. Retrying can't fix the file, so report
        # it and stop; the snapshot is untouched.
        logging.error("parser: %s rejected: %s", blob_name, exc)
        return

    for error in plan.errors:
        logging.warning("parser: %s rejected row: %s", blob_name, error)
    for item in plan.events:
        storage.send({**item, "source": blob_name})
    for item in plan.manager_events:
        storage.send({**item, "source": blob_name}, delay_seconds=MANAGER_DELAY_SECONDS)
    # Saved last: if anything above failed, Event Grid redelivers and the
    # same diff is computed again. Messages sent twice are harmless.
    storage.save_snapshot(plan.snapshot, blob_name, etag)

    counts = {}
    for item in plan.events:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    logging.info(
        "parser: %s -> %s; %d manager message(s); %d row(s) rejected",
        blob_name, ", ".join(f"{n} {t}" for t, n in sorted(counts.items())) or "no changes",
        len(plan.manager_events), len(plan.errors),
    )


@app.function_name(name="processor")
@app.queue_trigger(arg_name="msg", queue_name="%JML_QUEUE_NAME%", connection="AzureWebJobsStorage")
def processor(msg: func.QueueMessage) -> None:
    item = json.loads(msg.get_body().decode("utf-8"))
    employee_id = (item.get("employee") or {}).get("employee_id", "?")
    dry_run = _dry_run()
    try:
        actions = handle_event(_graph(), item, _config(), _group_ids(), dry_run)
    except Exception as exc:
        for action in getattr(exc, "actions", []):
            logging.info("audit %s", json.dumps(action, ensure_ascii=False))
        logging.error(
            "processor: %s %s failed on attempt %d: %s",
            item.get("type"), employee_id, msg.dequeue_count, exc,
        )
        raise   # the queue retries, then moves the message to the poison queue
    for action in actions:
        logging.info("audit %s", json.dumps(action, ensure_ascii=False))
    logging.info(
        "processor: %s %s done%s", item.get("type"), employee_id, " (dry run)" if dry_run else "",
    )
