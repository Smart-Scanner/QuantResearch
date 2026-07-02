from typing import Optional
from pydantic import BaseModel
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class RiskDecisionPayload(BaseModel):
    """
    The output of the Risk evaluation.
    """
    risk_decision_id: str
    recommendation_id: str
    decision_state: str # APPROVED, BLOCKED, APPROVED_REDUCED_SIZE
    reason: str

def create_risk_decision(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    risk_decision_id: str,
    recommendation_id: str,
    decision_state: str,
    reason: str
) -> DomainSnapshot:
    """Factory for the Risk Decision."""
    payload = RiskDecisionPayload(
        risk_decision_id=risk_decision_id,
        recommendation_id=recommendation_id,
        decision_state=decision_state,
        reason=reason
    )
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=risk_decision_id,
        snapshot_type="RiskDecision_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload.model_dump()
    )
