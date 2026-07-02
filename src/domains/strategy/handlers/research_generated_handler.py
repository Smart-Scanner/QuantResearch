from typing import Dict
from src.core.interfaces.storage import SnapshotStore, EventStore, RegistryStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.strategy.engine.base import StrategyEngine
from src.domains.strategy.strategies.momentum_v1 import MomentumStrategyV1
from src.domains.strategy.events.generated import StrategySignalGeneratedPayload

class ResearchSnapshotGeneratedHandler:
    def __init__(
        self, 
        snapshot_store: SnapshotStore, 
        event_store: EventStore, 
        registry_store: RegistryStore,
        bus: EventBus
    ):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.registry_store = registry_store
        self.bus = bus
        
        # Hardcoded registry of available engines for MVP
        self.engines: Dict[str, StrategyEngine] = {
            "momentum_v1": MomentumStrategyV1()
        }
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes ResearchSnapshotGenerated.
        Evaluates facts through active strategies and emits signals.
        """
        payload = event.payload
        snapshot_id = payload["snapshot_id"]
        
        # 1. Load the ResearchSnapshot
        research_snapshot = await self.snapshot_store.get(event.tenant_id, snapshot_id)
        if not research_snapshot:
            raise ValueError(f"ResearchSnapshot {snapshot_id} not found")
            
        # 2. Find active strategies in RegistryStore
        signals = []
        for strategy_id, engine in self.engines.items():
            status = await self.registry_store.get_status(event.tenant_id, strategy_id)
            if status != "ACTIVE":
                continue
                
            # 3. Evaluate facts against Active Strategy
            strategy_signals = engine.evaluate(
                research_snapshot.payload,
                correlation_id=event.correlation_id,
                causation_id=event.event_id # Research event caused the signal
            )
            signals.extend(strategy_signals)
            
        # 4. Save and Publish Signals
        for signal in signals:
            # Create payload
            signal_payload = StrategySignalGeneratedPayload(
                signal_id=signal.signal_id,
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                symbol=signal.symbol,
                signal_type=signal.signal_type,
                strength=signal.strength
            )
            
            # Create envelope
            generated_event = EventEnvelope(
                event_id=signal.signal_id, # Can reuse signal ID for event ID to avoid duplicate UUID generation
                event_type="StrategySignalGenerated",
                event_version="v1",
                correlation_id=signal.correlation_id,
                causation_id=signal.causation_id,
                tenant_id=event.tenant_id,
                portfolio_id=event.portfolio_id,
                payload=signal_payload.model_dump()
            )
            
            await self.event_store.append(generated_event)
            await self.bus.publish(generated_event)
