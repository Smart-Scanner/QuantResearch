from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from src.core.events.envelope import EventEnvelope
from src.core.contracts.base import DomainSnapshot, LedgerEntry, PolicyVersion

class EventStore(ABC):
    @abstractmethod
    async def append(self, event: EventEnvelope) -> None:
        """Append an immutable event to the store."""
        pass
        
    @abstractmethod
    async def read_stream(self, tenant_id: str, event_type: Optional[str] = None) -> List[EventEnvelope]:
        """Replay an event stream for a tenant."""
        pass

class SnapshotStore(ABC):
    @abstractmethod
    async def save(self, snapshot: DomainSnapshot) -> None:
        """Save a point-in-time snapshot payload."""
        pass
        
    @abstractmethod
    async def get(self, tenant_id: str, snapshot_id: str) -> Optional[DomainSnapshot]:
        """Retrieve a specific snapshot."""
        pass

class LedgerStore(ABC):
    @abstractmethod
    async def record_transaction(self, entry: LedgerEntry) -> None:
        """Append an entry to the financial ledger transactionally."""
        pass

class PolicyStore(ABC):
    @abstractmethod
    async def save_policy(self, policy: PolicyVersion) -> None:
        """Save a new version of a policy."""
        pass
        
    @abstractmethod
    async def get_active_policy(self, tenant_id: str, policy_id: str) -> Optional[PolicyVersion]:
        """Get the currently active policy version."""
        pass

class RegistryStore(ABC):
    @abstractmethod
    async def register(self, tenant_id: str, registry_type: str, registry_id: str, name: str, status: str, metadata: Dict[str, Any]) -> None:
        """Register a new long-lived identity."""
        pass
        
    @abstractmethod
    async def update_status(self, tenant_id: str, registry_id: str, status: str) -> None:
        """Update the mutable status of a registry item."""
        pass
        
    @abstractmethod
    async def get_status(self, tenant_id: str, registry_id: str) -> Optional[str]:
        """Get the status of a registry item."""
        pass
