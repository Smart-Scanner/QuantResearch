from pydantic import BaseModel
from src.domains.execution.models.order import OrderStatus

class ExecutionOrderCreatedPayload(BaseModel):
    order_id: str
    portfolio_decision_id: str
    symbol: str
    order_action: str
    quantity: int
    order_type: str
    status: OrderStatus

class ExecutionOrderSubmittedPayload(BaseModel):
    order_id: str
    status: OrderStatus

class ExecutionFillReceivedPayload(BaseModel):
    fill_id: str
    order_id: str
    symbol: str
    fill_action: str
    filled_quantity: int
    fill_price: float
    broker_reference: str
