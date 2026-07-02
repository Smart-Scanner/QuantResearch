from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.recommendation.engine.mvp_aggregator import MvpAggregator
from src.domains.recommendation.models.version import create_recommendation_version
from src.domains.recommendation.events.published import RecommendationPublishedPayload

class StrategySignalGeneratedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.aggregator = MvpAggregator()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes StrategySignalGenerated.
        1. Aggregates the signals into a Recommendation.
        2. Saves the RecommendationVersion to the SnapshotStore.
        3. Emits RecommendationPublished.
        """
        signal_payload = event.payload
        symbol = signal_payload["symbol"]
        
        # 1. Aggregate
        # For MVP, we treat each incoming signal event as the complete set of signals for that symbol.
        # In a multi-strategy future, this handler would either accumulate signals in a projection
        # and wait for an "AllSignalsComplete" event, or aggregate on the fly.
        recommendation_payload = self.aggregator.aggregate(symbol, [signal_payload])
        if not recommendation_payload:
            return
            
        # 2. Create and Save Snapshot
        recommendation_snapshot = create_recommendation_version(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            recommendation_id=recommendation_payload.recommendation_id,
            symbol=recommendation_payload.symbol,
            recommendation_state=recommendation_payload.recommendation_state,
            conviction=recommendation_payload.conviction,
            parent_strategy_signal_ids=recommendation_payload.parent_strategy_signal_ids,
            parent_recommendation_id=recommendation_payload.parent_recommendation_id,
            version=recommendation_payload.version
        )
        
        await self.snapshot_store.save(recommendation_snapshot)
        
        # 3. Create and Publish Event
        published_payload = RecommendationPublishedPayload(
            recommendation_id=recommendation_payload.recommendation_id,
            symbol=recommendation_payload.symbol,
            recommendation_state=recommendation_payload.recommendation_state,
            conviction=recommendation_payload.conviction,
            parent_strategy_signal_ids=recommendation_payload.parent_strategy_signal_ids
        )
        
        published_event = EventEnvelope(
            event_type="RecommendationPublished",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=published_payload.model_dump()
        )
        
        await self.event_store.append(published_event)
        await self.bus.publish(published_event)
