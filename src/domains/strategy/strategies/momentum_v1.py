import uuid
from typing import List
from src.domains.strategy.engine.base import StrategyEngine
from src.domains.strategy.models.signal import StrategySignal
from src.core.clock import DomainClock

class MomentumStrategyV1(StrategyEngine):
    """
    MVP Momentum Strategy.
    Rule: IF trend_state == BULLISH AND ma5 > ma20 THEN BUY
    """
    def __init__(self):
        self.strategy_id = "momentum_v1"
        self.strategy_version = "1"
        
    def evaluate(self, research_snapshot_payload: dict, correlation_id: str, causation_id: str) -> List[StrategySignal]:
        signals = []
        
        parent_snapshot_id = research_snapshot_payload.get("snapshot_id", "unknown")
        facts = research_snapshot_payload.get("facts", [])
        
        for fact in facts:
            symbol = fact["symbol"]
            trend_state = fact.get("trend_state", "BEARISH")
            ma5 = fact.get("ma5", 0)
            ma20 = fact.get("ma20", 0)
            
            signal_type = "AVOID"
            strength = 0
            
            if trend_state == "BULLISH" and ma5 > ma20:
                signal_type = "BUY"
                # Simple strength derivation from spread
                spread_pct = ((ma5 - ma20) / ma20) * 100
                # Cap strength between 50 and 100 for buy signals
                raw_strength = 50 + int(spread_pct * 10)
                strength = min(100, max(50, raw_strength))
            elif trend_state == "BEARISH" and ma5 < ma20:
                signal_type = "SELL"
                spread_pct = ((ma20 - ma5) / ma5) * 100
                raw_strength = 50 + int(spread_pct * 10)
                strength = min(100, max(50, raw_strength))
                
            signals.append(StrategySignal(
                signal_id=str(uuid.uuid4()),
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                signal_type=signal_type,
                strength=strength,
                parent_research_snapshot_id=parent_snapshot_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                created_at=DomainClock.utcnow()
            ))
            
        return signals
