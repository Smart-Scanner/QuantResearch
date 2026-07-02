import hashlib
import json
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class DataRecord(BaseModel):
    """Immutable market data record for a single symbol."""
    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int

class DataSnapshotPayload(BaseModel):
    """The payload structure inside the DomainSnapshot."""
    records: List[DataRecord]
    
    def generate_lineage_hash(self) -> str:
        """
        Generates a deterministic SHA-256 hash of the payload.
        Sorts symbols alphabetically to guarantee reproducibility.
        """
        # Sort by symbol to ensure deterministic hashing
        sorted_records = sorted(self.records, key=lambda x: x.symbol)
        
        # Convert to dictionary representations
        record_dicts = [record.model_dump() for record in sorted_records]
        
        # Serialize to strict JSON
        json_str = json.dumps(record_dicts, sort_keys=True, separators=(',', ':'))
        
        # SHA-256 hash
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()

def create_data_snapshot(
    tenant_id: str, 
    correlation_id: str, 
    causation_id: str, 
    records: List[DataRecord],
    snapshot_id: str
) -> DomainSnapshot:
    """Factory to create a well-formed DataSnapshot."""
    payload = DataSnapshotPayload(records=records)
    lineage_hash = payload.generate_lineage_hash()
    
    # Store lineage hash inside the payload dictionary for the wrapper
    payload_dict = payload.model_dump()
    payload_dict["lineage_hash"] = lineage_hash
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=snapshot_id,
        snapshot_type="DataSnapshot_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload_dict
    )
