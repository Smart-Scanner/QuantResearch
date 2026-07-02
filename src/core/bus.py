import asyncio
from typing import Callable, Dict, List, Awaitable
from src.core.events.envelope import EventEnvelope

EventHandler = Callable[[EventEnvelope], Awaitable[None]]

class EventBus:
    """
    In-Memory Async Event Bus for the Modular Monolith.
    Ensures type safety and correlation chain preservation.
    """
    def __init__(self):
        self._subscribers: Dict[str, List[EventHandler]] = {}
        
    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register an async handler for a specific event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        
    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler."""
        if event_type in self._subscribers:
            try:
                self._subscribers[event_type].remove(handler)
            except ValueError:
                pass
                
    async def publish(self, event: EventEnvelope) -> None:
        """
        Publish an event to the bus.
        Fire-and-forget dispatching to all registered handlers.
        """
        # Validate critical correlation fields are present
        if not event.correlation_id or not event.causation_id:
            raise ValueError(f"Event {event.event_type} is missing correlation envelope")
            
        handlers = self._subscribers.get(event.event_type, [])
        if not handlers:
            return
            
        # Dispatch concurrently to all handlers
        tasks = [asyncio.create_task(handler(event)) for handler in handlers]
        
        # We use gather to ensure we don't silently swallow exceptions during MVP, 
        # but in production, a dead-letter queue would handle failures.
        await asyncio.gather(*tasks, return_exceptions=False)
