"""The pipeline's own storage: the HR feed, the snapshot and the event queue.

All of it goes through the same credential as Microsoft Graph (the managed
identity in Azure). The storage account has account keys disabled, so there
is no connection string to use even by mistake.
"""

import json
from datetime import datetime, timezone

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

SNAPSHOT_BLOB = "snapshot.json"


class PipelineStorage:
    def __init__(self, account, credential, feed_container, state_container, queue_name):
        self.account = account
        self.feed_container = feed_container
        blobs = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential)
        self._feed = blobs.get_container_client(feed_container)
        self._snapshot = blobs.get_blob_client(state_container, SNAPSHOT_BLOB)
        # The Functions queue trigger expects base64 message bodies.
        self._queue = QueueClient(
            f"https://{account}.queue.core.windows.net", queue_name,
            credential=credential, message_encode_policy=TextBase64EncodePolicy(),
        )

    def read_feed(self, blob_name):
        """An HR export's text. Tolerates the byte-order mark Excel adds."""
        return self._feed.download_blob(blob_name).readall().decode("utf-8-sig")

    def load_snapshot(self):
        """(employees, etag) from the last processed export, or (None, None)
        before the first one."""
        try:
            download = self._snapshot.download_blob()
        except ResourceNotFoundError:
            return None, None
        data = json.loads(download.readall())
        return data.get("employees") or {}, download.properties.etag

    def save_snapshot(self, employees, source, etag):
        """Write the snapshot, but only if nobody else has since load.

        `etag` is what load_snapshot returned. If two exports are processed
        at once, the second writer's save fails instead of silently
        overwriting the first; its trigger then retries and diffs against
        the newer snapshot.
        """
        body = json.dumps({
            "source": source,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "employees": employees,
        }, ensure_ascii=False, indent=1)
        if etag is None:
            self._snapshot.upload_blob(body, overwrite=False)   # fails if one appeared
        else:
            self._snapshot.upload_blob(
                body, overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified,
            )

    def send(self, event, delay_seconds=0):
        """Queue one event, optionally invisible for the first delay_seconds."""
        self._queue.send_message(
            json.dumps(event, ensure_ascii=False), visibility_timeout=delay_seconds or None,
        )
