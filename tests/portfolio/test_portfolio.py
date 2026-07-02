import pytest
import asyncio
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from tests.core.fakes import FakeEventStore, FakeSnapshotStore
from src.core.contracts.base import DomainSnapshot
from src.core.clock import DomainClock
from src.domains.portfolio.handlers.risk_issued_handler import RiskDecisionIssuedHandler
from src.domains.recommendation.models.version import create_recommendation_version

@pytest.fixture
def setup_portfolio():
    bus = EventBus()
    event_store = FakeEventStore()
    snapshot_store = FakeSnapshotStore()
    handler = RiskDecisionIssuedHandler(snapshot_store, event_store, bus)
    
    # Pre-seed RecommendationSnapshot
    rec_snap = create_recommendation_version(
        "tenant-1", "corr-1", "cause-0", "rec-1", "RELIANCE", "BUY", "HIGH", ["sig-1"]
    )
    
    return handler, bus, event_store, snapshot_store, rec_snap

@pytest.mark.asyncio
async def test_portfolio_sizing(setup_portfolio):
    """Input: BUY HIGH APPROVED -> Output: OPEN_POSITION 10%"""
    handler, bus, event_store, snapshot_store, rec_snap = setup_portfolio
    await snapshot_store.save(rec_snap)
    
    risk_event = EventEnvelope(
        event_type="RiskDecisionIssued",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "risk_decision_id": "risk-1",
            "recommendation_id": "rec-1",
            "decision_state": "APPROVED",
            "reason": "OK"
        }
    )
    
    await handler.handle(risk_event)
    
    issued_event = event_store.events[0]
    payload = issued_event.payload
    
    assert payload["decision_type"] == "OPEN_POSITION"
    assert payload["target_weight"] == 10.0

@pytest.mark.asyncio
async def test_portfolio_blocked(setup_portfolio):
    """Input: BLOCKED -> Output: NO_ACTION"""
    handler, bus, event_store, snapshot_store, rec_snap = setup_portfolio
    await snapshot_store.save(rec_snap)
    
    risk_event = EventEnvelope(
        event_type="RiskDecisionIssued",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "risk_decision_id": "risk-1",
            "recommendation_id": "rec-1",
            "decision_state": "BLOCKED",
            "reason": "Too hot"
        }
    )
    
    await handler.handle(risk_event)
    
    issued_event = event_store.events[0]
    payload = issued_event.payload
    
    assert payload["decision_type"] == "NO_ACTION"
    assert payload["target_weight"] == 0.0

@pytest.mark.asyncio
async def test_portfolio_execution_purity(setup_portfolio):
    """Verify no execution leakage (no qty, broker, order_type)"""
    handler, bus, event_store, snapshot_store, rec_snap = setup_portfolio
    await snapshot_store.save(rec_snap)
    
    risk_event = EventEnvelope(
        event_type="RiskDecisionIssued",
        event_version="v1",
        correlation_id="corr-1",
        causation_id="cause-1",
        tenant_id="tenant-1",
        payload={
            "risk_decision_id": "risk-1",
            "recommendation_id": "rec-1",
            "decision_state": "APPROVED",
            "reason": "OK"
        }
    )
    
    await handler.handle(risk_event)
    
    issued_event = event_store.events[0]
    payload = issued_event.payload
    
    for key in payload.keys():
        assert "quantity" not in key.lower()
        assert "qty" not in key.lower()
        assert "broker" not in key.lower()
        assert "order_type" not in key.lower()
        assert "price" not in key.lower()
        assert "shares" not in key.lower()

@pytest.mark.asyncio
async def test_portfolio_traceability(setup_portfolio):
    """Golden Trace: CorrelationID preservation"""
    handler, bus, event_store, snapshot_store, rec_snap = setup_portfolio
    await snapshot_store.save(rec_snap)
    
    risk_event = EventEnvelope(
        event_type="RiskDecisionIssued",
        event_version="v1",
        correlation_id="PORTFOLIO_TRACE_999",
        causation_id="risk_event_123",
        tenant_id="tenant-1",
        payload={
            "risk_decision_id": "risk-1",
            "recommendation_id": "rec-1",
            "decision_state": "APPROVED",
            "reason": "OK"
        }
    )
    
    await handler.handle(risk_event)
    
    issued_event = event_store.events[0]
    assert issued_event.correlation_id == "PORTFOLIO_TRACE_999"
    assert issued_event.causation_id == risk_event.event_id
