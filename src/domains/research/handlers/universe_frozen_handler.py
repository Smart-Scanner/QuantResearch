import uuid
from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.research.services.calculator import ResearchCalculatorService
from src.domains.research.models.snapshot import create_research_snapshot
from src.domains.research.events.generated import ResearchSnapshotGeneratedPayload

class UniverseVersionFrozenHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.calculator_service = ResearchCalculatorService()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes UniverseVersionFrozen v1.
        1. Loads the UniverseSnapshot.
        2. Loads the DataSnapshot.
        3. Computes Research Facts for eligible symbols.
        4. Creates ResearchSnapshot.
        5. Emits ResearchSnapshotGenerated.
        """
        payload = event.payload
        universe_id = payload["universe_id"]
        parent_data_snapshot_id = payload["parent_data_snapshot_id"]
        symbols = payload["symbols"]
        
        # 1 & 2. Fetch dependencies
        data_snapshot = await self.snapshot_store.get(event.tenant_id, parent_data_snapshot_id)
        if not data_snapshot:
            raise ValueError(f"DataSnapshot {parent_data_snapshot_id} not found")
            
        # 3. Calculate facts
        records = data_snapshot.payload.get("records", [])
        facts = self.calculator_service.calculate_facts(symbols, records)
        
        # 4. Create ResearchSnapshot
        snapshot_id = str(uuid.uuid4())
        
        research_snapshot = create_research_snapshot(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id, # Universe event caused this
            snapshot_id=snapshot_id,
            universe_version_id=universe_id,
            data_snapshot_id=parent_data_snapshot_id,
            facts=facts
        )
        
        lineage_hash = research_snapshot.payload["lineage_hash"]
        
        await self.snapshot_store.save(research_snapshot)
        
        # 5. Emit Event
        generated_payload = ResearchSnapshotGeneratedPayload(
            snapshot_id=snapshot_id,
            snapshot_version="v1",
            universe_version_id=universe_id,
            data_snapshot_id=parent_data_snapshot_id,
            lineage_hash=lineage_hash
        )
        
        generated_event = EventEnvelope(
            event_type="ResearchSnapshotGenerated",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=generated_payload.model_dump()
        )
        
        await self.event_store.append(generated_event)
        await self.bus.publish(generated_event)
