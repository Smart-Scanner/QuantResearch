from typing import List, Optional, Dict, Any
from src.core.interfaces.storage import EventStore, SnapshotStore, LedgerStore, PolicyStore, RegistryStore
from src.core.events.envelope import EventEnvelope
from src.core.contracts.base import DomainSnapshot, LedgerEntry, PolicyVersion

class FakeEventStore(EventStore):
    def __init__(self):
        self.events: List[EventEnvelope] = []
        
    async def append(self, event: EventEnvelope) -> None:
        self.events.append(event)
        
    async def read_stream(self, tenant_id: str, event_type: Optional[str] = None) -> List[EventEnvelope]:
        stream = [e for e in self.events if e.tenant_id == tenant_id]
        if event_type:
            stream = [e for e in stream if e.event_type == event_type]
        return stream

class FakeSnapshotStore(SnapshotStore):
    def __init__(self):
        self.snapshots: Dict[str, DomainSnapshot] = {}
        
    async def save(self, snapshot: DomainSnapshot) -> None:
        key = f"{snapshot.tenant_id}::{snapshot.snapshot_id}"
        self.snapshots[key] = snapshot
        
    async def get(self, tenant_id: str, snapshot_id: str) -> Optional[DomainSnapshot]:
        key = f"{tenant_id}::{snapshot_id}"
        return self.snapshots.get(key)

class FakeLedgerStore(LedgerStore):
    def __init__(self):
        self.entries: List[LedgerEntry] = []
        
    async def record_transaction(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

class FakePolicyStore(PolicyStore):
    def __init__(self):
        self.policies: Dict[str, PolicyVersion] = {}
        
    async def save_policy(self, policy: PolicyVersion) -> None:
        key = f"{policy.tenant_id}::{policy.policy_id}"
        self.policies[key] = policy
        
    async def get_active_policy(self, tenant_id: str, policy_id: str) -> Optional[PolicyVersion]:
        key = f"{tenant_id}::{policy_id}"
        return self.policies.get(key)

class FakeRegistryStore(RegistryStore):
    def __init__(self):
        self.registry: Dict[str, Dict[str, Any]] = {}
        
    async def register(self, tenant_id: str, registry_type: str, registry_id: str, name: str, status: str, metadata: Dict[str, Any]) -> None:
        key = f"{tenant_id}::{registry_id}"
        self.registry[key] = {
            "type": registry_type,
            "name": name,
            "status": status,
            "metadata": metadata
        }
        
    async def update_status(self, tenant_id: str, registry_id: str, status: str) -> None:
        key = f"{tenant_id}::{registry_id}"
        if key in self.registry:
            self.registry[key]["status"] = status
            
    async def get_status(self, tenant_id: str, registry_id: str) -> Optional[str]:
        key = f"{tenant_id}::{registry_id}"
        if key in self.registry:
            return self.registry[key]["status"]
        return None
