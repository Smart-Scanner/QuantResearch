import uuid
from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.execution.engine.price_provider import StaticPriceProvider
from src.domains.execution.models.order import OrderStatus
from src.domains.execution.models.fill import create_execution_fill
from src.domains.execution.events.events import ExecutionFillReceivedPayload

class ExecutionOrderSubmittedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.price_provider = StaticPriceProvider()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes ExecutionOrderSubmitted.
        Simulates receiving a fill from the broker.
        Transitions Order to FILLED.
        Emits ExecutionFillReceived.
        """
        payload = event.payload
        order_id = payload["order_id"]
        
        # 1. Load Order
        order_snapshot = await self.snapshot_store.get(event.tenant_id, order_id)
        if not order_snapshot:
            return
            
        order_payload = order_snapshot.payload
        symbol = order_payload["symbol"]
        quantity = order_payload["quantity"]
        order_action = order_payload["order_action"]
        
        # 2. Get execution price (mocked)
        price = await self.price_provider.get_price(symbol)
        
        # 3. Create Fill Snapshot
        fill_id = str(uuid.uuid4())
        broker_ref = f"MOCK_BROKER_{fill_id[-8:]}"
        
        fill_snapshot = create_execution_fill(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            fill_id=fill_id,
            order_id=order_id,
            symbol=symbol,
            fill_action=order_action,
            filled_quantity=quantity,
            fill_price=float(price),
            broker_reference=broker_ref
        )
        await self.snapshot_store.save(fill_snapshot)
        
        # 4. Update Order Status
        order_snapshot.payload["status"] = OrderStatus.FILLED.value
        await self.snapshot_store.save(order_snapshot)
        
        # 5. Emit Fill Event
        fill_payload = ExecutionFillReceivedPayload(
            fill_id=fill_id,
            order_id=order_id,
            symbol=symbol,
            fill_action=order_action,
            filled_quantity=quantity,
            fill_price=float(price),
            broker_reference=broker_ref
        )
        
        fill_event = EventEnvelope(
            event_type="ExecutionFillReceived",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=fill_payload.model_dump()
        )
        
        await self.event_store.append(fill_event)
        await self.bus.publish(fill_event)
