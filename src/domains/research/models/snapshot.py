import hashlib
import json
from typing import List
from pydantic import BaseModel
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class ResearchFact(BaseModel):
    """
    Objective fact only. No opinions (e.g. BUY/SELL).
    """
    symbol: str
    close: float
    volume: int
    ma5: float
    ma20: float
    trend_state: str # BULLISH or BEARISH

class ResearchSnapshotPayload(BaseModel):
    universe_version_id: str
    data_snapshot_id: str
    facts: List[ResearchFact]
    
    def generate_lineage_hash(self) -> str:
        """Deterministic hash of the research facts."""
        sorted_facts = sorted(self.facts, key=lambda x: x.symbol)
        fact_dicts = [f.model_dump() for f in sorted_facts]
        
        data = {
            "universe_version_id": self.universe_version_id,
            "data_snapshot_id": self.data_snapshot_id,
            "facts": fact_dicts
        }
        json_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()

def create_research_snapshot(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    snapshot_id: str,
    universe_version_id: str,
    data_snapshot_id: str,
    facts: List[ResearchFact]
) -> DomainSnapshot:
    payload = ResearchSnapshotPayload(
        universe_version_id=universe_version_id,
        data_snapshot_id=data_snapshot_id,
        facts=facts
    )
    
    lineage_hash = payload.generate_lineage_hash()
    payload_dict = payload.model_dump()
    payload_dict["lineage_hash"] = lineage_hash
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=snapshot_id,
        snapshot_type="ResearchSnapshot_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload_dict
    )
