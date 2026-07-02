import hashlib
import json
from typing import List
from pydantic import BaseModel
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class UniverseSnapshotPayload(BaseModel):
    universe_id: str
    universe_version: str
    symbols: List[str]
    parent_data_snapshot_id: str
    
    def generate_lineage_hash(self) -> str:
        """Deterministic hash of the universe symbols."""
        sorted_symbols = sorted(self.symbols)
        data = {
            "parent_data_snapshot_id": self.parent_data_snapshot_id,
            "symbols": sorted_symbols
        }
        json_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()

def create_universe_snapshot(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    universe_id: str,
    universe_version: str,
    symbols: List[str],
    parent_data_snapshot_id: str
) -> DomainSnapshot:
    payload = UniverseSnapshotPayload(
        universe_id=universe_id,
        universe_version=universe_version,
        symbols=symbols,
        parent_data_snapshot_id=parent_data_snapshot_id
    )
    
    lineage_hash = payload.generate_lineage_hash()
    payload_dict = payload.model_dump()
    payload_dict["lineage_hash"] = lineage_hash
    payload_dict["symbol_count"] = len(symbols)
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=universe_id,
        snapshot_type="UniverseSnapshot_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload_dict
    )
