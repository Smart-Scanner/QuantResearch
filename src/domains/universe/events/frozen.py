from typing import List
from pydantic import BaseModel

class UniverseVersionFrozenPayload(BaseModel):
    """
    Payload for the UniverseVersionFrozen v1 event.
    """
    universe_id: str
    universe_version: str
    symbols: List[str]
    parent_data_snapshot_id: str
    lineage_hash: str
