from pydantic import BaseModel

class ResearchSnapshotGeneratedPayload(BaseModel):
    """
    Payload for the ResearchSnapshotGenerated v1 event.
    """
    snapshot_id: str
    snapshot_version: str
    universe_version_id: str
    data_snapshot_id: str
    lineage_hash: str
