import pytest
import asyncio
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore, FakeSnapshotStore
from src.domains.market_data.models.snapshot import create_data_snapshot, DataRecord
from src.domains.universe.models.snapshot import create_universe_snapshot
from src.domains.research.handlers.universe_frozen_handler import UniverseVersionFrozenHandler

@pytest.fixture
def setup_research():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    handler = UniverseVersionFrozenHandler(snapshot_store, event_store, bus)
    
    # Pre-seed DataSnapshot
    records = [
        DataRecord(symbol="GOOD_STOCK", date="2026-06-21", open=100, high=105, low=98, close=104, volume=100000),
        DataRecord(symbol="ANOTHER_STOCK", date="2026-06-21", open=50, high=52, low=48, close=51, volume=60000)
    ]
    data_snapshot = create_data_snapshot("tenant-1", "corr-1", "cause-0", records, "data-snap-1")
    
    # Pre-seed UniverseSnapshot
    universe_snapshot = create_universe_snapshot(
        "tenant-1", "corr-1", "cause-1", "univ-snap-1", "v1", ["GOOD_STOCK", "ANOTHER_STOCK"], "data-snap-1"
    )
    
    return handler, bus, event_store, snapshot_store, data_snapshot, universe_snapshot

@pytest.mark.asyncio
async def test_research_reproducibility(setup_research):
    """Same DataSnapshot + UniverseVersion = same ResearchSnapshot hash."""
    handler, bus, event_store, snapshot_store, data_snapshot, universe_snapshot = setup_research
    
    await snapshot_store.save(data_snapshot)
    await snapshot_store.save(universe_snapshot)
    
    universe_event = EventEnvelope(
        event_type="UniverseVersionFrozen",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "universe_id": "univ-snap-1",
            "universe_version": "v1",
            "symbols": ["GOOD_STOCK", "ANOTHER_STOCK"],
            "parent_data_snapshot_id": "data-snap-1",
            "lineage_hash": "univ-hash"
        }
    )
    
    # First run
    await handler.handle(universe_event)
    hash_1 = event_store.events[0].payload["lineage_hash"]
    
    # Second run
    await handler.handle(universe_event)
    hash_2 = event_store.events[1].payload["lineage_hash"]
    
    assert hash_1 == hash_2

@pytest.mark.asyncio
async def test_research_domain_purity(setup_research):
    """Research output contains facts only, no opinions."""
    handler, bus, event_store, snapshot_store, data_snapshot, universe_snapshot = setup_research
    await snapshot_store.save(data_snapshot)
    await snapshot_store.save(universe_snapshot)
    
    universe_event = EventEnvelope(
        event_type="UniverseVersionFrozen",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "universe_id": "univ-snap-1",
            "parent_data_snapshot_id": "data-snap-1",
            "symbols": ["GOOD_STOCK"]
        }
    )
    
    await handler.handle(universe_event)
    
    research_snapshot_id = event_store.events[0].payload["snapshot_id"]
    research_snapshot = await snapshot_store.get("tenant-1", research_snapshot_id)
    
    fact = research_snapshot.payload["facts"][0]
    
    assert "symbol" in fact
    assert "close" in fact
    assert "volume" in fact
    assert "ma5" in fact
    assert "ma20" in fact
    assert fact["trend_state"] in ["BULLISH", "BEARISH"]
    
    # No opinions allowed
    for key in fact.keys():
        assert "buy" not in key.lower()
        assert "sell" not in key.lower()
        assert "score" not in key.lower()

@pytest.mark.asyncio
async def test_research_correlation_traceability(setup_research):
    """
    DataSnapshot -> UniverseVersion -> ResearchSnapshot
    Single CorrelationID regenerates the chain.
    """
    handler, bus, event_store, snapshot_store, data_snapshot, universe_snapshot = setup_research
    await snapshot_store.save(data_snapshot)
    await snapshot_store.save(universe_snapshot)
    
    universe_event = EventEnvelope(
        event_type="UniverseVersionFrozen",
        event_version="v1",
        correlation_id="GOLDEN_TRACE_444",
        causation_id="univ_frozen_event_888",
        tenant_id="tenant-1",
        payload={
            "universe_id": "univ-snap-1",
            "parent_data_snapshot_id": "data-snap-1",
            "symbols": ["GOOD_STOCK"]
        }
    )
    
    await handler.handle(universe_event)
    
    research_event = event_store.events[0]
    
    assert research_event.correlation_id == "GOLDEN_TRACE_444"
    assert research_event.causation_id == universe_event.event_id
    assert research_event.payload["universe_version_id"] == "univ-snap-1"
    assert research_event.payload["data_snapshot_id"] == "data-snap-1"
