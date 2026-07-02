import uuid
from src.domains.risk.engine.base import RiskEngine
from src.domains.risk.models.decision import RiskDecisionPayload

class MvpRiskEngine(RiskEngine):
    """
    MVP Risk Engine.
    Enforces a single constraint: MAX_PORTFOLIO_HEAT = 80%
    """
    MAX_PORTFOLIO_HEAT = 80.0
    
    def evaluate(self, recommendation_payload: dict, portfolio_snapshot: dict) -> RiskDecisionPayload:
        recommendation_id = recommendation_payload["recommendation_id"]
        
        # In a full system, we'd check symbol-specific limits, sector heat, correlation matrix, etc.
        # For MVP, we simply check global portfolio heat.
        current_heat = portfolio_snapshot.get("portfolio_heat", 0.0)
        
        if current_heat > self.MAX_PORTFOLIO_HEAT:
            decision_state = "BLOCKED"
            reason = f"Portfolio heat {current_heat}% exceeds limit {self.MAX_PORTFOLIO_HEAT}%"
        else:
            decision_state = "APPROVED"
            reason = "Risk limits passed"
            
        return RiskDecisionPayload(
            risk_decision_id=str(uuid.uuid4()),
            recommendation_id=recommendation_id,
            decision_state=decision_state,
            reason=reason
        )
