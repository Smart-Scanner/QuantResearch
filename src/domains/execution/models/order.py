from enum import Enum
from pydantic import BaseModel
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock

class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

class ExecutionOrderPayload(BaseModel):
    """
    The intended trade execution.
    """
    order_id: str
    portfolio_decision_id: str
    idempotency_key: str
    
    symbol: str
    order_action: str # BUY, SELL
    quantity: int
    order_type: str # MARKET, LIMIT
    
    status: OrderStatus

def create_execution_order(
    tenant_id: str,
    correlation_id: str,
    causation_id: str,
    order_id: str,
    portfolio_decision_id: str,
    idempotency_key: str,
    symbol: str,
    order_action: str,
    quantity: int,
    order_type: str,
    status: OrderStatus
) -> DomainSnapshot:
    payload = ExecutionOrderPayload(
        order_id=order_id,
        portfolio_decision_id=portfolio_decision_id,
        idempotency_key=idempotency_key,
        symbol=symbol,
        order_action=order_action,
        quantity=quantity,
        order_type=order_type,
        status=status
    )
    
    return DomainSnapshot(
        tenant_id=tenant_id,
        snapshot_id=order_id,
        snapshot_type="ExecutionOrder_v1",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=DomainClock.utcnow(),
        payload=payload.model_dump()
    )
