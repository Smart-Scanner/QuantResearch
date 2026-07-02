import uuid
from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.universe.services.filter import UniverseFilterService
from src.domains.universe.models.snapshot import create_universe_snapshot
from src.domains.universe.events.frozen import UniverseVersionFrozenPayload

class DataSnapshotPublishedHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.filter_service = UniverseFilterService()
        
    async def handle(self, event: EventEnvelope) -> None:
        """
        Consumes DataSnapshotPublished v1.
        1. Loads the DataSnapshot from the SnapshotStore.
        2. Filters eligible symbols.
        3. Creates UniverseSnapshot.
        4. Emits UniverseVersionFrozen.
        """
        payload = event.payload
        parent_data_snapshot_id = payload["snapshot_id"]
        
        # 1. Fetch parent snapshot
        data_snapshot = await self.snapshot_store.get(event.tenant_id, parent_data_snapshot_id)
        if not data_snapshot:
            raise ValueError(f"DataSnapshot {parent_data_snapshot_id} not found in store")
            
        # 2. Filter symbols
        records = data_snapshot.payload.get("records", [])
        eligible_symbols = self.filter_service.filter_eligible_symbols(records)
        
        # 3. Create UniverseSnapshot
        universe_id = str(uuid.uuid4())
        universe_version = "v1" # Hardcoded for MVP, in reality driven by PolicyVersion
        
        universe_snapshot = create_universe_snapshot(
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
            causation_id=event.event_id, # The data event caused this
            universe_id=universe_id,
            universe_version=universe_version,
            symbols=eligible_symbols,
            parent_data_snapshot_id=parent_data_snapshot_id
        )
        
        lineage_hash = universe_snapshot.payload["lineage_hash"]
        
        await self.snapshot_store.save(universe_snapshot)
        
        # 4. Create and publish event
        frozen_payload = UniverseVersionFrozenPayload(
            universe_id=universe_id,
            universe_version=universe_version,
            symbols=eligible_symbols,
            parent_data_snapshot_id=parent_data_snapshot_id,
            lineage_hash=lineage_hash
        )
        
        frozen_event = EventEnvelope(
            event_type="UniverseVersionFrozen",
            event_version="v1",
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            tenant_id=event.tenant_id,
            portfolio_id=event.portfolio_id,
            payload=frozen_payload.model_dump()
        )
        
        await self.event_store.append(frozen_event)
        await self.bus.publish(frozen_event)
