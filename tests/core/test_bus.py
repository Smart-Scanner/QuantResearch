import pytest
import asyncio
from datetime import datetime, timezone
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore

@pytest.mark.asyncio
async def test_event_bus_dispatch_preserves_correlation():
    bus = EventBus()
    store = FakeEventStore()
    
    # Simple handler that saves to fake store
    async def save_handler(event: EventEnvelope):
        await store.append(event)
        
    bus.subscribe("FakeDataSnapshotPublished", save_handler)
    
    # 1. Create DataSnapshotPublished
    data_event = EventEnvelope(
        event_type="FakeDataSnapshotPublished",
        event_version="v1",
        correlation_id="corr-123",
        causation_id="cause-123",
        tenant_id="tenant-1",
        payload={"symbol": "RELIANCE", "price": 100}
    )
    
    await bus.publish(data_event)
    
    # Verify store recorded the event
    assert len(store.events) == 1
    stored_event = store.events[0]
    
    # Verify strict correlation preservation
    assert stored_event.correlation_id == "corr-123"
    assert stored_event.causation_id == "cause-123"
    assert stored_event.event_type == "FakeDataSnapshotPublished"

@pytest.mark.asyncio
async def test_event_bus_enforces_correlation_fields():
    bus = EventBus()
    
    # Missing correlation_id/causation_id should raise ValueError
    try:
        invalid_event = EventEnvelope(
            event_type="FakeDataSnapshotPublished",
            event_version="v1",
            correlation_id="",  # Invalid
            causation_id="",    # Invalid
            tenant_id="tenant-1",
            payload={}
        )
        await bus.publish(invalid_event)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "missing correlation envelope" in str(e)

@pytest.mark.asyncio
async def test_full_correlation_chain():
    bus = EventBus()
    store = FakeEventStore()
    
    # Handler for step 1
    async def step1_handler(event: EventEnvelope):
        await store.append(event)
        # Create dependent event (Step 2)
        step2_event = EventEnvelope(
            event_type="ResearchSnapshotGenerated",
            event_version="v1",
            correlation_id=event.correlation_id,       # Preserved!
            causation_id=event.event_id,               # Causation points to parent event!
            tenant_id=event.tenant_id,
            payload={"thesis": "Buy"}
        )
        await bus.publish(step2_event)
        
    # Handler for step 2
    async def step2_handler(event: EventEnvelope):
        await store.append(event)
        
    bus.subscribe("FakeDataSnapshotPublished", step1_handler)
    bus.subscribe("ResearchSnapshotGenerated", step2_handler)
    
    initial_event = EventEnvelope(
        event_type="FakeDataSnapshotPublished",
        event_version="v1",
        correlation_id="corr-999",
        causation_id="trigger-000",
        tenant_id="tenant-1",
        payload={}
    )
    
    await bus.publish(initial_event)
    
    # Both events should be in the store
    assert len(store.events) == 2
    
    # Verify the chain
    e1, e2 = store.events[0], store.events[1]
    
    assert e1.event_type == "FakeDataSnapshotPublished"
    assert e2.event_type == "ResearchSnapshotGenerated"
    
    assert e1.correlation_id == e2.correlation_id == "corr-999"
    assert e2.causation_id == e1.event_id
