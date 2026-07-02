import uuid
from src.core.interfaces.storage import SnapshotStore, EventStore
from src.core.bus import EventBus
from src.core.events.envelope import EventEnvelope
from src.domains.market_data.commands.ingest import TriggerEodIngestion
from src.domains.market_data.services.ingestion import MockIngestionService
from src.domains.market_data.models.snapshot import create_data_snapshot
from src.domains.market_data.events.published import DataSnapshotPublishedPayload

class EodIngestionHandler:
    def __init__(self, snapshot_store: SnapshotStore, event_store: EventStore, bus: EventBus):
        self.snapshot_store = snapshot_store
        self.event_store = event_store
        self.bus = bus
        self.ingestion_service = MockIngestionService()
        
    async def handle(self, command: TriggerEodIngestion) -> None:
        """
        Handles the TriggerEodIngestion command.
        1. Fetches data
        2. Creates snapshot
        3. Saves snapshot to SnapshotStore
        4. Appends event to EventStore
        5. Publishes event to EventBus
        """
        # 1. Fetch data
        records = self.ingestion_service.fetch_eod_data(command.target_date)
        
        # 2. Create snapshot (Calculates lineage hash internally)
        snapshot_id = str(uuid.uuid4())
        snapshot = create_data_snapshot(
            tenant_id=command.tenant_id,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id, # The command itself is the cause
            records=records,
            snapshot_id=snapshot_id
        )
        
        lineage_hash = snapshot.payload["lineage_hash"]
        
        # 3. Save to SnapshotStore
        await self.snapshot_store.save(snapshot)
        
        # 4. Create Event Payload
        event_payload = DataSnapshotPublishedPayload(
            snapshot_id=snapshot_id,
            snapshot_version="v1",
            universe_size=len(records),
            lineage_hash=lineage_hash
        )
        
        # 5. Create Event Envelope
        event = EventEnvelope(
            event_type="DataSnapshotPublished",
            event_version="v1",
            correlation_id=command.correlation_id,
            causation_id=command.causation_id, # Causation is the command
            tenant_id=command.tenant_id,
            portfolio_id=command.portfolio_id,
            payload=event_payload.model_dump()
        )
        
        # 6. Save and Publish
        await self.event_store.append(event)
        await self.bus.publish(event)
