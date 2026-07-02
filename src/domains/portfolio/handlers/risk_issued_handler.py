from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.portfolio.engine.mvp_portfolio import MvpPortfolioEngine
from src.domains.portfolio.models.snapshot import create_portfolio_decision
from src.domains.portfolio.events.published import PortfolioDecisionPublishedPayload

class RiskDecisionIssuedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.portfolio_engine = MvpPortfolioEngine()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes RiskDecisionIssued.
        1. Loads the associated Recommendation.
        2. Loads Portfolio Snapshot.
        3. Calculates Target Weight.
        4. Saves PortfolioDecision.
        5. Emits PortfolioDecisionPublished.
        """
        risk_payload = event.payload
        recommendation_id = risk_payload["recommendation_id"]
        
        # 1. Fetch associated Recommendation
        recommendation_snapshot = await self.snapshot_store.get(event.tenant_id, recommendation_id)
        if not recommendation_snapshot:
            raise ValueError(f"Recommendation {recommendation_id} not found")
            
        # 2. Fetch Portfolio Snapshot
        portfolio_snapshot = await self.snapshot_store.get(event.tenant_id, "mock-portfolio")
        portfolio_payload = portfolio_snapshot.payload if portfolio_snapshot else {}
        
        # 3. Evaluate Portfolio Target Weight
        decision_payload = self.portfolio_engine.allocate(
            risk_payload, 
            recommendation_snapshot.payload, 
            portfolio_payload
        )
        
        # 4. Create and Save Snapshot
        decision_snapshot = create_portfolio_decision(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            portfolio_decision_id=decision_payload.portfolio_decision_id,
            recommendation_id=decision_payload.recommendation_id,
            risk_decision_id=decision_payload.risk_decision_id,
            decision_type=decision_payload.decision_type,
            target_weight=decision_payload.target_weight,
            allocated_capital=decision_payload.allocated_capital,
            rationale=decision_payload.rationale
        )
        
        await self.snapshot_store.save(decision_snapshot)
        
        # 5. Create and Publish Event
        published_payload = PortfolioDecisionPublishedPayload(
            portfolio_decision_id=decision_payload.portfolio_decision_id,
            recommendation_id=decision_payload.recommendation_id,
            risk_decision_id=decision_payload.risk_decision_id,
            decision_type=decision_payload.decision_type,
            target_weight=decision_payload.target_weight,
            allocated_capital=decision_payload.allocated_capital,
            rationale=decision_payload.rationale
        )
        
        published_event = EventEnvelope(
            event_type="PortfolioDecisionPublished",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=published_payload.model_dump()
        )
        
        await self.event_store.append(published_event)
        await self.bus.publish(published_event)
