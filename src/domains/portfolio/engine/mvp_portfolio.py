import uuid
from src.domains.portfolio.engine.base import PortfolioEngine
from src.domains.portfolio.models.snapshot import PortfolioDecisionPayload

class MvpPortfolioEngine(PortfolioEngine):
    """
    MVP Portfolio Engine.
    Hardcoded MVP sizing policy: HIGH=10%, MEDIUM=5%, LOW=2%.
    """
    
    def allocate(self, risk_decision_payload: dict, recommendation_payload: dict, portfolio_snapshot: dict) -> PortfolioDecisionPayload:
        risk_decision_id = risk_decision_payload["risk_decision_id"]
        recommendation_id = recommendation_payload["recommendation_id"]
        
        decision_state = risk_decision_payload.get("decision_state")
        recommendation_state = recommendation_payload.get("recommendation_state")
        conviction = recommendation_payload.get("conviction")
        
        target_weight = 0.0
        allocated_capital = 0.0
        decision_type = "NO_ACTION"
        rationale = "Risk Blocked"
        
        cash_balance = portfolio_snapshot.get("cash_balance", 0.0)
        
        if decision_state == "APPROVED":
            if recommendation_state == "BUY":
                decision_type = "OPEN_POSITION"
                if conviction == "HIGH":
                    target_weight = 10.0
                elif conviction == "MEDIUM":
                    target_weight = 5.0
                else:
                    target_weight = 2.0
                rationale = f"Approved {conviction} Conviction Buy"
                allocated_capital = cash_balance * (target_weight / 100.0)
            elif recommendation_state == "AVOID":
                # For MVP, avoid might mean close position if we had positions state
                # But since we don't have positions built yet, we'll just emit NO_ACTION
                # Or maybe CLOSE_POSITION with 0.0 target weight
                decision_type = "NO_ACTION"
                target_weight = 0.0
                rationale = "Avoid state"
        
        return PortfolioDecisionPayload(
            portfolio_decision_id=str(uuid.uuid4()),
            recommendation_id=recommendation_id,
            risk_decision_id=risk_decision_id,
            decision_type=decision_type,
            target_weight=target_weight,
            allocated_capital=allocated_capital,
            rationale=rationale
        )
