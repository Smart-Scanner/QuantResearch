import uuid
from typing import List, Optional
from src.domains.recommendation.engine.base import RecommendationAggregator
from src.domains.recommendation.models.version import RecommendationVersionPayload

class MvpAggregator(RecommendationAggregator):
    """
    MVP Aggregation logic.
    For MVP, assumes only 1 strategy exists per symbol.
    Maps: Strength -> Conviction
    """
    def aggregate(self, symbol: str, signal_payloads: List[dict]) -> Optional[RecommendationVersionPayload]:
        if not signal_payloads:
            return None
            
        # In MVP, there is only one strategy signal per symbol
        signal = signal_payloads[0]
        signal_id = signal["signal_id"]
        strength = signal.get("strength", 0)
        
        # 1. Map State
        raw_signal_type = signal.get("signal_type", "AVOID")
        # Strategy 'BUY/SELL/AVOID' maps naturally to Recommendation 'BUY/HOLD/AVOID' in MVP
        recommendation_state = raw_signal_type if raw_signal_type in ["BUY", "AVOID"] else "HOLD"
        # Wait, if strategy says SELL, in MVP we map to HOLD? The user said Recommendation MVP uses BUY/HOLD/AVOID.
        # So BUY->BUY, SELL->AVOID, AVOID->HOLD ? Or maybe SELL -> AVOID.
        if raw_signal_type == "SELL":
            recommendation_state = "AVOID"
        elif raw_signal_type == "AVOID":
            recommendation_state = "HOLD"
        
        # Actually user said: Strategy may emit BUY/SELL/AVOID. Recommendation state: BUY/HOLD/AVOID.
        # BUY -> BUY. SELL -> AVOID. AVOID -> HOLD. Let's just do a direct mapping for simplicity:
        if raw_signal_type == "BUY":
            recommendation_state = "BUY"
        elif raw_signal_type == "SELL":
            recommendation_state = "AVOID"
        else:
            recommendation_state = "HOLD"
            
        # 2. Map Conviction
        conviction = "LOW"
        if strength >= 80:
            conviction = "HIGH"
        elif strength >= 65:
            conviction = "MEDIUM"
            
        return RecommendationVersionPayload(
            recommendation_id=str(uuid.uuid4()),
            symbol=symbol,
            recommendation_state=recommendation_state,
            conviction=conviction,
            parent_strategy_signal_ids=[signal_id],
            version=1
        )
