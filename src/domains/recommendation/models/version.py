from typing import List, Optional
from pydantic import BaseModel
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class RecommendationVersionPayload(BaseModel):
    """
    The unified investment opinion synthesized from alpha signals.
    """
    recommendation_id: str
    symbol: str
    
    recommendation_state: str # BUY/HOLD/AVOID
    conviction: str # LOW/MEDIUM/HIGH
    
    parent_strategy_signal_ids: List[str]
    parent_recommendation_id: Optional[str] = None
    version: int = 1

def create_recommendation_version(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    recommendation_id: str,
    symbol: str,
    recommendation_state: str,
    conviction: str,
    parent_strategy_signal_ids: List[str],
    parent_recommendation_id: Optional[str] = None,
    version: int = 1
) -> DomainSnapshot:
    """Factory for the Recommendation Snapshot."""
    payload = RecommendationVersionPayload(
        recommendation_id=recommendation_id,
        symbol=symbol,
        recommendation_state=recommendation_state,
        conviction=conviction,
        parent_strategy_signal_ids=parent_strategy_signal_ids,
        parent_recommendation_id=parent_recommendation_id,
        version=version
    )
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=recommendation_id,
        snapshot_type="RecommendationVersion_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload.model_dump()
    )
