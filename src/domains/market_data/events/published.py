from pydantic import BaseModel
from typing import Dict, Any

class DataSnapshotPublishedPayload(BaseModel):
    """
    Payload for the DataSnapshotPublished v1 event.
    Must ONLY contain metadata, not the actual 500MB data.
    """
    snapshot_id: str
    snapshot_version: str
    universe_size: int
    lineage_hash: str
