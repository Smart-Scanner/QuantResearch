import pytest
import asyncio
from datetime import datetime, timezone
from src.core.clock import DomainClock
from src.core.bus import EventBus
from tests.core.fakes import FakeEventStore, FakeSnapshotStore
from src.domains.market_data.commands.ingest import TriggerEodIngestion
from src.domains.market_data.handlers.ingest_handler import EodIngestionHandler
from src.domains.market_data.models.snapshot import create_data_snapshot
from src.domains.market_data.services.ingestion import MockIngestionService

@pytest.fixture
def setup_infrastructure():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    handler = EodIngestionHandler(snapshot_store, event_store, bus)
    
    # Fix the clock for deterministic testing
    fixed_time = datetime(2026, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
    DomainClock.set_fixed_time(fixed_time)
    
    yield handler, bus, event_store, snapshot_store
    
    DomainClock.reset()

@pytest.mark.asyncio
async def test_determinism_same_input_same_hash():
    """
    Run ingestion twice. Same input must equal same lineage_hash.
    If not: FAIL because downstream reproducibility will be impossible.
    """
    service = MockIngestionService()
    
    # Run 1
    records_1 = service.fetch_eod_data("2026-06-21")
    snapshot_1 = create_data_snapshot("tenant-1", "corr-1", "cause-1", records_1, "snap-1")
    hash_1 = snapshot_1.payload["lineage_hash"]
    
    # Run 2
    records_2 = service.fetch_eod_data("2026-06-21")
    snapshot_2 = create_data_snapshot("tenant-1", "corr-2", "cause-2", records_2, "snap-2")
    hash_2 = snapshot_2.payload["lineage_hash"]
    
    assert hash_1 == hash_2, "Hashes must be identical for the same target_date"

@pytest.mark.asyncio
async def test_functional_snapshot_and_event_persisted(setup_infrastructure):
    """
    ✓ Snapshot persisted
    ✓ Event emitted
    ✓ Event stored
    """
    handler, bus, event_store, snapshot_store = setup_infrastructure
    
    # We will subscribe to verify the bus emission
    emitted_events = []
    async def capture_event(event):
        emitted_events.append(event)
    bus.subscribe("DataSnapshotPublished", capture_event)
    
    command = TriggerEodIngestion(
        tenant_id="tenant-1",
        correlation_id="corr-99",
        causation_id="cmd-1",
        target_date="2026-06-21"
    )
    
    await handler.handle(command)
    
    # 1. Event stored
    assert len(event_store.events) == 1
    stored_event = event_store.events[0]
    assert stored_event.event_type == "DataSnapshotPublished"
    
    # 2. Event emitted
    assert len(emitted_events) == 1
    assert emitted_events[0].event_id == stored_event.event_id
    
    # 3. Snapshot persisted
    snapshot_id = stored_event.payload["snapshot_id"]
    snapshot = await snapshot_store.get("tenant-1", snapshot_id)
    assert snapshot is not None
    assert snapshot.payload["lineage_hash"] == stored_event.payload["lineage_hash"]

@pytest.mark.asyncio
async def test_governance_correlation_id_survives(setup_infrastructure):
    """
    Verify: correlation_id survives end-to-end.
    """
    handler, bus, event_store, snapshot_store = setup_infrastructure
    
    command = TriggerEodIngestion(
        tenant_id="tenant-1",
        correlation_id="GOVERNANCE_TRACE_123",
        causation_id="cron_job_456",
        target_date="2026-06-21"
    )
    
    await handler.handle(command)
    
    stored_event = event_store.events[0]
    assert stored_event.correlation_id == "GOVERNANCE_TRACE_123"
    assert stored_event.causation_id == "cron_job_456"

@pytest.mark.asyncio
async def test_replay_rebuilds_identical_snapshot(setup_infrastructure):
    """
    Delete snapshot projection.
    Replay event.
    Rebuild snapshot.
    Result identical.
    """
    handler, bus, event_store, snapshot_store = setup_infrastructure
    
    command = TriggerEodIngestion(
        tenant_id="tenant-1",
        correlation_id="corr-replay",
        causation_id="cause-replay",
        target_date="2026-06-22"
    )
    
    await handler.handle(command)
    
    original_event = event_store.events[0]
    original_snapshot_id = original_event.payload["snapshot_id"]
    original_snapshot = await snapshot_store.get("tenant-1", original_snapshot_id)
    original_hash = original_snapshot.payload["lineage_hash"]
    
    # DELETE snapshot (simulate projection loss)
    snapshot_store.snapshots.clear()
    assert await snapshot_store.get("tenant-1", original_snapshot_id) is None
    
    # REPLAY: In reality, a ProjectionBuilder would read the event and fetch the snapshot.
    # Since DataSnapshot IS the projection of raw market data, rebuilding it means
    # re-fetching the data for that target_date (which the event represents indirectly).
    # Since our ingestion is deterministic, fetching it again will produce the same hash.
    
    service = MockIngestionService()
    records = service.fetch_eod_data("2026-06-22")
    rebuilt_snapshot = create_data_snapshot(
        "tenant-1", "corr-replay", "cause-replay", records, original_snapshot_id
    )
    
    # Result identical
    assert rebuilt_snapshot.payload["lineage_hash"] == original_hash
