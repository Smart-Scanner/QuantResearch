import pytest
import asyncio
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore, FakeSnapshotStore
from src.domains.market_data.models.snapshot import create_data_snapshot, DataRecord
from src.domains.universe.handlers.data_published_handler import DataSnapshotPublishedHandler

@pytest.fixture
def setup_universe():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    handler = DataSnapshotPublishedHandler(snapshot_store, event_store, bus)
    
    # Pre-seed a DataSnapshot into the store
    records = [
        DataRecord(symbol="GOOD_STOCK", date="2026-06-21", open=100, high=105, low=98, close=104, volume=100000),
        DataRecord(symbol="LOW_VOL", date="2026-06-21", open=100, high=105, low=98, close=104, volume=10000),
        DataRecord(symbol="LOW_PRICE", date="2026-06-21", open=10, high=12, low=9, close=11, volume=100000)
    ]
    data_snapshot = create_data_snapshot("tenant-1", "corr-1", "cause-1", records, "data-snap-1")
    
    return handler, bus, event_store, snapshot_store, data_snapshot

@pytest.mark.asyncio
async def test_universe_determinism(setup_universe):
    """Same DataSnapshot = same UniverseVersion, same symbols, same hash."""
    handler, bus, event_store, snapshot_store, data_snapshot = setup_universe
    await snapshot_store.save(data_snapshot)
    
    event_payload = {
        "snapshot_id": "data-snap-1",
        "snapshot_version": "v1",
        "universe_size": 3,
        "lineage_hash": "mock-hash"
    }
    
    data_event = EventEnvelope(
        event_type="DataSnapshotPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cmd-1",
        tenant_id="tenant-1",
        payload=event_payload
    )
    
    # First execution
    await handler.handle(data_event)
    frozen_event_1 = event_store.events[0]
    hash_1 = frozen_event_1.payload["lineage_hash"]
    
    # Second execution
    await handler.handle(data_event)
    frozen_event_2 = event_store.events[1]
    hash_2 = frozen_event_2.payload["lineage_hash"]
    
    assert hash_1 == hash_2
    assert frozen_event_1.payload["symbols"] == ["GOOD_STOCK"]

@pytest.mark.asyncio
async def test_universe_replay_recreates_identically(setup_universe):
    handler, bus, event_store, snapshot_store, data_snapshot = setup_universe
    await snapshot_store.save(data_snapshot)
    
    data_event = EventEnvelope(
        event_type="DataSnapshotPublished",
        event_version="v1",
        correlation_id="corr-replay",
        causation_id="cmd-replay",
        tenant_id="tenant-1",
        payload={"snapshot_id": "data-snap-1"}
    )
    
    await handler.handle(data_event)
    original_event = event_store.events[0]
    universe_id = original_event.payload["universe_id"]
    original_hash = original_event.payload["lineage_hash"]
    
    # Destroy the projection
    del snapshot_store.snapshots[f"tenant-1::{universe_id}"]
    
    # Replay
    await handler.handle(data_event)
    rebuilt_event = event_store.events[1]
    rebuilt_universe_id = rebuilt_event.payload["universe_id"]
    
    rebuilt_snapshot = await snapshot_store.get("tenant-1", rebuilt_universe_id)
    assert rebuilt_snapshot.payload["lineage_hash"] == original_hash

@pytest.mark.asyncio
async def test_universe_lineage_audit(setup_universe):
    handler, bus, event_store, snapshot_store, data_snapshot = setup_universe
    await snapshot_store.save(data_snapshot)
    
    data_event = EventEnvelope(
        event_type="DataSnapshotPublished",
        event_version="v1",
        correlation_id="AUDIT_TRACE_999",
        causation_id="cause_123",
        tenant_id="tenant-1",
        payload={"snapshot_id": "data-snap-1"}
    )
    
    await handler.handle(data_event)
    frozen_event = event_store.events[0]
    
    assert frozen_event.correlation_id == "AUDIT_TRACE_999"
    assert frozen_event.causation_id == data_event.event_id
    assert frozen_event.payload["parent_data_snapshot_id"] == "data-snap-1"
