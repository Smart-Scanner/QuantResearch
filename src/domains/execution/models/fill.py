from pydantic import BaseModel
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class ExecutionFillPayload(BaseModel):
    """
    The actual filled execution from the market.
    """
    fill_id: str
    order_id: str
    
    symbol: str
    fill_action: str # BUY, SELL
    filled_quantity: int
    fill_price: float
    
    broker_reference: str

def create_execution_fill(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    fill_id: str,
    order_id: str,
    symbol: str,
    fill_action: str,
    filled_quantity: int,
    fill_price: float,
    broker_reference: str
) -> DomainSnapshot:
    payload = ExecutionFillPayload(
        fill_id=fill_id,
        order_id=order_id,
        symbol=symbol,
        fill_action=fill_action,
        filled_quantity=filled_quantity,
        fill_price=fill_price,
        broker_reference=broker_reference
    )
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=fill_id,
        snapshot_type="ExecutionFill_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload.model_dump()
    )
