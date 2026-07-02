from pydantic import BaseModel

class StrategySignalGeneratedPayload(BaseModel):
    """
    Payload for the StrategySignalGenerated v1 event.
    """
    signal_id: str
    
    strategy_id: str
    strategy_version: str
    
    symbol: str
    signal_type: str
    strength: int
