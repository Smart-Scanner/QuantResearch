import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from src.core.clock import DomainClock

def generate_uuid() -> str:
    return str(uuid.uuid4())

class EventEnvelope(BaseModel):
    """
    The absolute most important file in the system.
    If this changes later, MarketOS migration pain starts.
    """
    event_id: str = Field(default_factory=generate_uuid)
    event_type: str
    event_version: str
    
    correlation_id: str
    causation_id: str
    
    tenant_id: str
    portfolio_id: Optional[str] = None
    
    occurred_at: datetime = Field(default_factory=DomainClock.utcnow)
    payload: Dict[str, Any]
