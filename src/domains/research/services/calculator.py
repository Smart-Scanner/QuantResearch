from typing import List, Dict, Any
from src.domains.research.models.snapshot import ResearchFact

class ResearchCalculatorService:
    """
    Computes objective mathematical facts from raw data.
    """
    def calculate_facts(self, universe_symbols: List[str], data_records: List[Dict[str, Any]]) -> List[ResearchFact]:
        facts = []
        # In a real system, we'd have historical arrays to compute MAs. 
        # For the MVP static slice, we will simulate the MAs deterministically based on the single day's close.
        # This proves the architectural flow without needing 20 days of historical data seeding.
        
        for record in data_records:
            symbol = record["symbol"]
            if symbol not in universe_symbols:
                continue
                
            close = record["close"]
            volume = record["volume"]
            
            # Deterministic mock calculation for MVP architecture validation
            # Real implementation would use TA-Lib or Pandas over historical Series
            ma5 = round(close * 0.98, 2)  # Simulate MA5 slightly below close
            ma20 = round(close * 0.95, 2) # Simulate MA20 further below close
            
            trend_state = "BULLISH" if close > ma20 else "BEARISH"
            
            facts.append(ResearchFact(
                symbol=symbol,
                close=close,
                volume=volume,
                ma5=ma5,
                ma20=ma20,
                trend_state=trend_state
            ))
            
        return sorted(facts, key=lambda x: x.symbol)
