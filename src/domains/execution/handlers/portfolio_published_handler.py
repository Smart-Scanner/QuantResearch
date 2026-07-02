import uuid
from decimal import Decimal
from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.execution.engine.price_provider import PriceProvider, StaticPriceProvider
from src.domains.execution.models.order import create_execution_order, OrderStatus
from src.domains.execution.events.events import ExecutionOrderCreatedPayload

class PortfolioDecisionPublishedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.price_provider = StaticPriceProvider()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes PortfolioDecisionPublished.
        1. Checks idempotency.
        2. Calculates quantity based on allocated_capital and current price.
        3. Creates ExecutionOrder (CREATED).
        4. Emits ExecutionOrderCreated.
        """
        decision_payload = event.payload
        portfolio_decision_id = decision_payload["portfolio_decision_id"]
        decision_type = decision_payload["decision_type"]
        allocated_capital = decision_payload.get("allocated_capital", 0.0)
        
        if decision_type == "NO_ACTION" or allocated_capital <= 0:
            return
            
        idempotency_key = f"exec_order_{portfolio_decision_id}"
        
        # 1. Idempotency Check
        # If an order with this idempotency key exists, skip.
        # In MVP, we check the event store or snapshot store for this key.
        # For a quick MVP idempotency check without adding custom query methods:
        # We assume the snapshot_id is deterministic or we check if it already exists.
        # Let's make the order_id deterministic based on idempotency_key to prevent duplicates.
        order_id = str(uuid.uuid5(uuid.NAMESPACE_OID, idempotency_key))
        existing_order = await self.snapshot_store.get(event.tenant_id, order_id)
        if existing_order:
            return # Already processed
            
        # We need the symbol. The PortfolioDecision only has recommendation_id.
        # We must load the Recommendation to get the symbol.
        recommendation_id = decision_payload["recommendation_id"]
        rec_snapshot = await self.snapshot_store.get(event.tenant_id, recommendation_id)
        if not rec_snapshot:
            raise ValueError(f"Recommendation {recommendation_id} not found")
        symbol = rec_snapshot.payload["symbol"]
        
        # 2. Get Price & Calculate Quantity
        price = await self.price_provider.get_price(symbol)
        quantity = int(allocated_capital / float(price))
        if quantity <= 0:
            return
            
        order_action = "BUY" if decision_type in ["OPEN_POSITION", "ADD_POSITION"] else "SELL"
        
        # 3. Create & Save Order Snapshot
        order_snapshot = create_execution_order(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            order_id=order_id,
            portfolio_decision_id=portfolio_decision_id,
            idempotency_key=idempotency_key,
            symbol=symbol,
            order_action=order_action,
            quantity=quantity,
            order_type="MARKET",
            status=OrderStatus.CREATED
        )
        await self.snapshot_store.save(order_snapshot)
        
        # 4. Emit Event
        created_payload = ExecutionOrderCreatedPayload(
            order_id=order_id,
            portfolio_decision_id=portfolio_decision_id,
            symbol=symbol,
            order_action=order_action,
            quantity=quantity,
            order_type="MARKET",
            status=OrderStatus.CREATED
        )
        
        created_event = EventEnvelope(
            event_type="ExecutionOrderCreated",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=created_payload.model_dump()
        )
        
        await self.event_store.append(created_event)
        await self.bus.publish(created_event)
