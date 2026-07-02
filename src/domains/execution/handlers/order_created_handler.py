from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.execution.models.order import OrderStatus
from src.domains.execution.events.events import ExecutionOrderSubmittedPayload

class ExecutionOrderCreatedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes ExecutionOrderCreated.
        Transitions state to SUBMITTED.
        """
        payload = event.payload
        order_id = payload["order_id"]
        
        # Load Order
        order_snapshot = await self.snapshot_store.get(event.tenant_id, order_id)
        if not order_snapshot:
            return
            
        # Update State
        order_snapshot.payload["status"] = OrderStatus.SUBMITTED.value
        await self.snapshot_store.save(order_snapshot)
        
        # Emit Submitted
        submitted_payload = ExecutionOrderSubmittedPayload(
            order_id=order_id,
            status=OrderStatus.SUBMITTED
        )
        
        submitted_event = EventEnvelope(
            event_type="ExecutionOrderSubmitted",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=submitted_payload.model_dump()
        )
        
        await self.event_store.append(submitted_event)
        await self.bus.publish(submitted_event)
