from typing import Optional
from pydantic import BaseModel
from datetime import datetime
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class PortfolioSnapshotPayload(BaseModel):
    """
    State of the Portfolio at a point in time.
    """
    portfolio_id: str
    cash_balance: float
    invested_capital: float
    gross_exposure: float
    portfolio_heat: float
    generated_at: datetime

def create_portfolio_snapshot(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    portfolio_id: str,
    cash_balance: float,
    invested_capital: float,
    gross_exposure: float,
    portfolio_heat: float
) -> DomainSnapshot:
    """Factory for Portfolio Snapshot."""
    payload = PortfolioSnapshotPayload(
        portfolio_id=portfolio_id,
        cash_balance=cash_balance,
        invested_capital=invested_capital,
        gross_exposure=gross_exposure,
        portfolio_heat=portfolio_heat,
        generated_at=DomainClock.utcnow()
    )
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=portfolio_id,
        snapshot_type="PortfolioSnapshot_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload.model_dump()
    )

class PortfolioDecisionPayload(BaseModel):
    """
    The output of the Portfolio evaluation. Target weight only, no execution details.
    """
    portfolio_decision_id: str
    recommendation_id: str
    risk_decision_id: str
    decision_type: str # OPEN_POSITION, ADD_POSITION, REDUCE_POSITION, CLOSE_POSITION, NO_ACTION
    target_weight: float # percentage (e.g. 10.0 for 10%)
    allocated_capital: float # absolute capital allocation (e.g. 10000.0)
    rationale: str

def create_portfolio_decision(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    portfolio_decision_id: str,
    recommendation_id: str,
    risk_decision_id: str,
    decision_type: str,
    target_weight: float,
    allocated_capital: float,
    rationale: str
) -> DomainSnapshot:
    """Factory for the Portfolio Decision."""
    payload = PortfolioDecisionPayload(
        portfolio_decision_id=portfolio_decision_id,
        recommendation_id=recommendation_id,
        risk_decision_id=risk_decision_id,
        decision_type=decision_type,
        target_weight=target_weight,
        allocated_capital=allocated_capital,
        rationale=rationale
    )
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=portfolio_decision_id,
        snapshot_type="PortfolioDecision_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload.model_dump()
    )
