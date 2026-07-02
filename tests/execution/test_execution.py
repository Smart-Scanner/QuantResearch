import pytest
import asyncio
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore, FakeSnapshotStore
from src.domains.recommendation.models.version import create_recommendation_version
from src.domains.execution.handlers.portfolio_published_handler import PortfolioDecisionPublishedHandler
from src.domains.execution.handlers.order_created_handler import ExecutionOrderCreatedHandler
from src.domains.execution.handlers.order_submitted_handler import ExecutionOrderSubmittedHandler

@pytest.fixture
def setup_execution():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    
    portfolio_handler = PortfolioDecisionPublishedHandler(snapshot_store, event_store, bus)
    created_handler = ExecutionOrderCreatedHandler(snapshot_store, event_store, bus)
    submitted_handler = ExecutionOrderSubmittedHandler(snapshot_store, event_store, bus)
    
    # Pre-seed RecommendationSnapshot for symbol lookup
    rec_snap = create_recommendation_version(
        "tenant-1", "corr-1", "cause-0", "rec-1", "RELIANCE", "BUY", "HIGH", ["sig-1"]
    )
    
    return bus, event_store, snapshot_store, portfolio_handler, created_handler, submitted_handler, rec_snap

@pytest.mark.asyncio
async def test_execution_state_machine(setup_execution):
    """CREATED -> SUBMITTED -> FILLED"""
    bus, event_store, snapshot_store, portfolio_handler, created_handler, submitted_handler, rec_snap = setup_execution
    await snapshot_store.save(rec_snap)
    
    portfolio_event = EventEnvelope(
        event_type="PortfolioDecisionPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "portfolio_decision_id": "port-1",
            "recommendation_id": "rec-1",
            "risk_decision_id": "risk-1",
            "decision_type": "OPEN_POSITION",
            "target_weight": 10.0,
            "allocated_capital": 10000.0, # 10000 / 500 = 20 qty
            "rationale": "OK"
        }
    )
    
    # 1. Handle Portfolio Decision
    await portfolio_handler.handle(portfolio_event)
    created_event = event_store.events[0]
    assert created_event.event_type == "ExecutionOrderCreated"
    assert created_event.payload["status"] == "CREATED"
    assert created_event.payload["quantity"] == 20
    
    order_id = created_event.payload["order_id"]
    order_snap = await snapshot_store.get("tenant-1", order_id)
    assert order_snap.payload["status"] == "CREATED"
    
    # 2. Handle Order Created
    await created_handler.handle(created_event)
    submitted_event = event_store.events[1]
    assert submitted_event.event_type == "ExecutionOrderSubmitted"
    
    order_snap = await snapshot_store.get("tenant-1", order_id)
    assert order_snap.payload["status"] == "SUBMITTED"
    
    # 3. Handle Order Submitted
    await submitted_handler.handle(submitted_event)
    fill_event = event_store.events[2]
    assert fill_event.event_type == "ExecutionFillReceived"
    assert fill_event.payload["filled_quantity"] == 20
    
    order_snap = await snapshot_store.get("tenant-1", order_id)
    assert order_snap.payload["status"] == "FILLED"

@pytest.mark.asyncio
async def test_execution_idempotency(setup_execution):
    """Same PortfolioDecision processed twice = 1 order only."""
    bus, event_store, snapshot_store, portfolio_handler, created_handler, submitted_handler, rec_snap = setup_execution
    await snapshot_store.save(rec_snap)
    
    portfolio_event = EventEnvelope(
        event_type="PortfolioDecisionPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "portfolio_decision_id": "port-2",
            "recommendation_id": "rec-1",
            "risk_decision_id": "risk-1",
            "decision_type": "OPEN_POSITION",
            "target_weight": 10.0,
            "allocated_capital": 5000.0,
            "rationale": "OK"
        }
    )
    
    # Process First Time
    await portfolio_handler.handle(portfolio_event)
    assert len(event_store.events) == 1
    
    # Process Second Time
    await portfolio_handler.handle(portfolio_event)
    assert len(event_store.events) == 1 # Still 1 event!

@pytest.mark.asyncio
async def test_execution_correlation(setup_execution):
    """Golden trace reaches ExecutionFill with same correlation_id."""
    bus, event_store, snapshot_store, portfolio_handler, created_handler, submitted_handler, rec_snap = setup_execution
    await snapshot_store.save(rec_snap)
    
    portfolio_event = EventEnvelope(
        event_type="PortfolioDecisionPublished",
        event_version="v1",
        correlation_id="EXECUTION_TRACE_FINAL",
        causation_id="portfolio_event_id",
        tenant_id="tenant-1",
        payload={
            "portfolio_decision_id": "port-3",
            "recommendation_id": "rec-1",
            "risk_decision_id": "risk-1",
            "decision_type": "OPEN_POSITION",
            "target_weight": 10.0,
            "allocated_capital": 5000.0,
            "rationale": "OK"
        }
    )
    
    await portfolio_handler.handle(portfolio_event)
    created_event = event_store.events[0]
    
    await created_handler.handle(created_event)
    submitted_event = event_store.events[1]
    
    await submitted_handler.handle(submitted_event)
    fill_event = event_store.events[2]
    
    assert fill_event.correlation_id == "EXECUTION_TRACE_FINAL"

@pytest.mark.asyncio
async def test_execution_replay(setup_execution):
    """Drop Execution projection. Replay events. Regenerate identical state."""
    bus, event_store, snapshot_store, portfolio_handler, created_handler, submitted_handler, rec_snap = setup_execution
    await snapshot_store.save(rec_snap)
    
    portfolio_event = EventEnvelope(
        event_type="PortfolioDecisionPublished",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "portfolio_decision_id": "port-4",
            "recommendation_id": "rec-1",
            "risk_decision_id": "risk-1",
            "decision_type": "OPEN_POSITION",
            "target_weight": 10.0,
            "allocated_capital": 10000.0,
            "rationale": "OK"
        }
    )
    
    await portfolio_handler.handle(portfolio_event)
    created_event = event_store.events[0]
    
    order_id = created_event.payload["order_id"]
    original_snap = await snapshot_store.get("tenant-1", order_id)
    
    # Wipe projection & event
    del snapshot_store.snapshots[f"tenant-1::{order_id}"]
    event_store.events.clear()
    
    # Replay
    await portfolio_handler.handle(portfolio_event)
    replayed_snap = await snapshot_store.get("tenant-1", order_id)
    
    assert original_snap.payload == replayed_snap.payload
