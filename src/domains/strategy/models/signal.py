from pydantic import BaseModel
from src.core.clock import DomainClock
from datetime import datetime

class StrategySignal(BaseModel):
    """
    The definitive evaluation of objective facts.
    """
    signal_id: str
    
    strategy_id: str
    strategy_version: str
    
    symbol: str
    signal_type: str # "BUY", "SELL", "AVOID"
    strength: int # 0 to 100
    
    parent_research_snapshot_id: str
    
    correlation_id: str
    causation_id: str
    
    created_at: datetime
