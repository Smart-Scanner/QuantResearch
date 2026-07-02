import pytest
import asyncio
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore, FakeSnapshotStore
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock
from src.domains.risk.handlers.recommendation_published_handler import RecommendationPublishedHandler

@pytest.fixture
def setup_risk():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    handler = RecommendationPublishedHandler(snapshot_store, event_store, bus)
    return handler, bus, event_store, snapshot_store

@pytest.mark.asyncio
async def test_risk_approval(setup_risk):
    """Heat 50% -> Result: APPROVED"""
    handler, bus, event_store, snapshot_store = setup_risk
    
    # Mock Portfolio Snapshot with heat 50
    portfolio_snap = DomainSnapshot(
        tenant_id="tenant-1",
        snapshot_id="mock-portfolio",
        snapshot_type="PortfolioSnapshot_v1",
        correlation_id="c1",
        causation_id="c2",
        created_at=DomainClock.utcnow(),
        payload={"portfolio_heat": 50.0}
    )
    await snapshot_store.save(portfolio_snap)
    
    rec_event = EventEnvelope(
        event_type="RecommendationPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "recommendation_id": "rec-1",
            "symbol": "RELIANCE",
            "recommendation_state": "BUY",
            "conviction": "HIGH",
            "parent_strategy_signal_ids": ["sig-1"]
        }
    )
    
    await handler.handle(rec_event)
    
    issued_event = event_store.events[0]
    assert issued_event.payload["decision_state"] == "APPROVED"

@pytest.mark.asyncio
async def test_risk_blocked(setup_risk):
    """Heat 90% -> Result: BLOCKED"""
    handler, bus, event_store, snapshot_store = setup_risk
    
    portfolio_snap = DomainSnapshot(
        tenant_id="tenant-1",
        snapshot_id="mock-portfolio",
        snapshot_type="PortfolioSnapshot_v1",
        correlation_id="c1",
        causation_id="c2",
        created_at=DomainClock.utcnow(),
        payload={"portfolio_heat": 90.0}
    )
    await snapshot_store.save(portfolio_snap)
    
    rec_event = EventEnvelope(
        event_type="RecommendationPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "recommendation_id": "rec-1",
            "symbol": "RELIANCE",
            "recommendation_state": "BUY",
            "conviction": "HIGH",
            "parent_strategy_signal_ids": ["sig-1"]
        }
    )
    
    await handler.handle(rec_event)
    
    issued_event = event_store.events[0]
    assert issued_event.payload["decision_state"] == "BLOCKED"

@pytest.mark.asyncio
async def test_risk_traceability(setup_risk):
    """Verify CorrelationID is preserved."""
    handler, bus, event_store, snapshot_store = setup_risk
    
    rec_event = EventEnvelope(
        event_type="RecommendationPublished",
        event_version="v1",
        correlation_id="GLOBAL_TRACE_ID_100",
        causation_id="recommendation_event_ID",
        tenant_id="tenant-1",
        payload={
            "recommendation_id": "rec-1",
            "symbol": "RELIANCE",
            "recommendation_state": "BUY",
            "conviction": "HIGH",
            "parent_strategy_signal_ids": ["sig-1"]
        }
    )
    
    await handler.handle(rec_event)
    
    issued_event = event_store.events[0]
    assert issued_event.correlation_id == "GLOBAL_TRACE_ID_100"
    assert issued_event.causation_id == rec_event.event_id

@pytest.mark.asyncio
async def test_risk_replay(setup_risk):
    """Delete Risk projection, replay event, generate identical RiskDecision."""
    handler, bus, event_store, snapshot_store = setup_risk
    
    rec_event = EventEnvelope(
        event_type="RecommendationPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "recommendation_id": "rec-1",
            "symbol": "RELIANCE",
            "recommendation_state": "BUY",
            "conviction": "HIGH",
            "parent_strategy_signal_ids": ["sig-1"]
        }
    )
    
    await handler.handle(rec_event)
    
    original_event = event_store.events[0]
    risk_id = original_event.payload["risk_decision_id"]
    original_snapshot = await snapshot_store.get("tenant-1", risk_id)
    
    # Delete projection
    del snapshot_store.snapshots[f"tenant-1::{risk_id}"]
    
    # Replay event
    await handler.handle(rec_event)
    
    rebuilt_event = event_store.events[1]
    rebuilt_risk_id = rebuilt_event.payload["risk_decision_id"]
    rebuilt_snapshot = await snapshot_store.get("tenant-1", rebuilt_risk_id)
    
    assert rebuilt_snapshot.payload["decision_state"] == original_snapshot.payload["decision_state"]
    assert rebuilt_snapshot.payload["reason"] == original_snapshot.payload["reason"]
    assert rebuilt_snapshot.payload["recommendation_id"] == original_snapshot.payload["recommendation_id"]
