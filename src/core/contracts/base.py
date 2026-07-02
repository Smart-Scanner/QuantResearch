from pydantic import BaseModel
from typing import Dict, Any, Optional
from datetime import datetime

class DomainEvent(BaseModel):
    """Base model for all domain events before they are enveloped."""
    pass

class DomainCommand(BaseModel):
    """Base model for all domain commands representing intent."""
    tenant_id: str
    portfolio_id: Optional[str] = None
    correlation_id: str
    causation_id: str

class DomainSnapshot(BaseModel):
    """Base model for heavy, point-in-time materialized payloads."""
    tenant_id: str
    snapshot_id: str
    snapshot_type: str
    correlation_id: str
    causation_id: str
    created_at: datetime
    payload: Dict[str, Any]

class LedgerEntry(BaseModel):
    """Strict financial accounting entry."""
    tenant_id: str
    account_id: str
    ledger_entry_id: str
    symbol: str
    delta_qty: float
    delta_cash: float
    event_source_id: str
    occurred_at: datetime
    correlation_id: str
    causation_id: str

class PolicyVersion(BaseModel):
    """Versioned rules and parameters."""
    tenant_id: str
    policy_id: str
    version: str
    parent_version: Optional[str]
    effective_from: datetime
    effective_to: Optional[datetime]
    payload: Dict[str, Any]
    correlation_id: str
    causation_id: str
