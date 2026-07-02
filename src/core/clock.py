from datetime import datetime, timezone

class DomainClock:
    """
    Global clock for the domain.
    Allows for deterministic testing and event replay by injecting fixed times.
    """
    _fixed_time = None
    
    @classmethod
    def set_fixed_time(cls, dt: datetime):
        cls._fixed_time = dt
        
    @classmethod
    def reset(cls):
        cls._fixed_time = None
        
    @classmethod
    def utcnow(cls) -> datetime:
        if cls._fixed_time is not None:
            return cls._fixed_time
        return datetime.now(timezone.utc)
