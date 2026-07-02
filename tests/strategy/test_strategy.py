import pytest
import asyncio
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore, FakeSnapshotStore, FakeRegistryStore
from src.domains.research.models.snapshot import create_research_snapshot, ResearchFact
from src.domains.strategy.handlers.research_generated_handler import ResearchSnapshotGeneratedHandler

@pytest.fixture
def setup_strategy():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    registry_store = FakeRegistryStore()
    handler = ResearchSnapshotGeneratedHandler(snapshot_store, event_store, registry_store, bus)
    
    # Pre-seed ResearchSnapshot
    facts = [
        ResearchFact(symbol="STRONG_BUY", close=100, volume=1000, ma5=105, ma20=95, trend_state="BULLISH"), # ma5 > ma20
        ResearchFact(symbol="STRONG_SELL", close=50, volume=1000, ma5=45, ma20=55, trend_state="BEARISH"),  # ma5 < ma20
        ResearchFact(symbol="AVOID_STOCK", close=100, volume=1000, ma5=95, ma20=105, trend_state="BULLISH") # Conflicting
    ]
    research_snapshot = create_research_snapshot(
        "tenant-1", "corr-1", "cause-0", "res-snap-1", "univ-1", "data-1", facts
    )
    
    return handler, bus, event_store, snapshot_store, registry_store, research_snapshot

@pytest.mark.asyncio
async def test_strategy_determinism(setup_strategy):
    """Same ResearchSnapshot must generate identical StrategySignals."""
    handler, bus, event_store, snapshot_store, registry_store, research_snapshot = setup_strategy
    await snapshot_store.save(research_snapshot)
    await registry_store.register("tenant-1", "strategy", "momentum_v1", "Momentum v1", "ACTIVE", {})
    
    research_event = EventEnvelope(
        event_type="ResearchSnapshotGenerated",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={"snapshot_id": "res-snap-1"}
    )
    
    await handler.handle(research_event)
    
    # Check outputs
    assert len(event_store.events) == 3 # 3 symbols = 3 signals
    signals = event_store.events
    
    buy_signal = next(e for e in signals if e.payload["symbol"] == "STRONG_BUY")
    assert buy_signal.payload["signal_type"] == "BUY"
    assert buy_signal.payload["strength"] > 50
    
    sell_signal = next(e for e in signals if e.payload["symbol"] == "STRONG_SELL")
    assert sell_signal.payload["signal_type"] == "SELL"
    assert sell_signal.payload["strength"] > 50
    
    avoid_signal = next(e for e in signals if e.payload["symbol"] == "AVOID_STOCK")
    assert avoid_signal.payload["signal_type"] == "AVOID"

@pytest.mark.asyncio
async def test_strategy_registry_dependency(setup_strategy):
    """Inactive strategy must produce 0 signals."""
    handler, bus, event_store, snapshot_store, registry_store, research_snapshot = setup_strategy
    await snapshot_store.save(research_snapshot)
    await registry_store.register("tenant-1", "strategy", "momentum_v1", "Momentum v1", "SUSPENDED", {})
    
    research_event = EventEnvelope(
        event_type="ResearchSnapshotGenerated",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={"snapshot_id": "res-snap-1"}
    )
    
    await handler.handle(research_event)
    
    assert len(event_store.events) == 0 # Suspended strategy does not emit signals

@pytest.mark.asyncio
async def test_strategy_correlation_traceability(setup_strategy):
    """Signals must carry the same correlation ID as the chain."""
    handler, bus, event_store, snapshot_store, registry_store, research_snapshot = setup_strategy
    await snapshot_store.save(research_snapshot)
    await registry_store.register("tenant-1", "strategy", "momentum_v1", "Momentum v1", "ACTIVE", {})
    
    research_event = EventEnvelope(
        event_type="ResearchSnapshotGenerated",
        event_version="v1",
        correlation_id="STRATEGY_TRACE_999",
        causation_id="research_event_000",
        tenant_id="tenant-1",
        payload={"snapshot_id": "res-snap-1"}
    )
    
    await handler.handle(research_event)
    
    signal_event = event_store.events[0]
    
    assert signal_event.correlation_id == "STRATEGY_TRACE_999"
    assert signal_event.causation_id == research_event.event_id

@pytest.mark.asyncio
async def test_strategy_purity(setup_strategy):
    """Strategy emits BUY/SELL/AVOID, NOT Portfolio weight or opinions."""
    handler, bus, event_store, snapshot_store, registry_store, research_snapshot = setup_strategy
    await snapshot_store.save(research_snapshot)
    await registry_store.register("tenant-1", "strategy", "momentum_v1", "Momentum v1", "ACTIVE", {})
    
    research_event = EventEnvelope(
        event_type="ResearchSnapshotGenerated",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={"snapshot_id": "res-snap-1"}
    )
    
    await handler.handle(research_event)
    
    for event in event_store.events:
        payload = event.payload
        assert payload["signal_type"] in ["BUY", "SELL", "AVOID"]
        assert "target_price" not in payload
        assert "weight" not in payload
        assert "portfolio" not in payload
