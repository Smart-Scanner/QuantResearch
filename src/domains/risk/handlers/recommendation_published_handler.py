from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.risk.engine.mvp_risk import MvpRiskEngine
from src.domains.risk.models.decision import create_risk_decision
from src.domains.risk.events.issued import RiskDecisionIssuedPayload

class RecommendationPublishedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.risk_engine = MvpRiskEngine()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes RecommendationPublished.
        1. Loads the Portfolio Snapshot.
        2. Evaluates the recommendation against Risk Limits.
        3. Saves RiskDecision.
        4. Emits RiskDecisionIssued.
        """
        recommendation_payload = event.payload
        
        # 1. Fetch current portfolio risk state
        # In MVP, since we haven't built the Portfolio domain yet, we simulate the portfolio snapshot.
        # We will use the snapshot store to retrieve a mock portfolio snapshot if it exists.
        portfolio_snapshot = await self.snapshot_store.get(event.tenant_id, "mock-portfolio")
        portfolio_payload = portfolio_snapshot.payload if portfolio_snapshot else {"portfolio_heat": 50.0}
        
        # 2. Evaluate
        decision_payload = self.risk_engine.evaluate(recommendation_payload, portfolio_payload)
        
        # 3. Create and Save Snapshot
        decision_snapshot = create_risk_decision(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            risk_decision_id=decision_payload.risk_decision_id,
            recommendation_id=decision_payload.recommendation_id,
            decision_state=decision_payload.decision_state,
            reason=decision_payload.reason
        )
        
        await self.snapshot_store.save(decision_snapshot)
        
        # 4. Create and Publish Event
        issued_payload = RiskDecisionIssuedPayload(
            risk_decision_id=decision_payload.risk_decision_id,
            recommendation_id=decision_payload.recommendation_id,
            decision_state=decision_payload.decision_state,
            reason=decision_payload.reason
        )
        
        issued_event = EventEnvelope(
            event_type="RiskDecisionIssued",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=issued_payload.model_dump()
        )
        
        await self.event_store.append(issued_event)
        await self.bus.publish(issued_event)
