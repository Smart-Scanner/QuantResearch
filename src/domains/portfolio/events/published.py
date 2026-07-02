from pydantic import BaseModel

class PortfolioDecisionPublishedPayload(BaseModel):
    """
    Payload for the PortfolioDecisionPublished v1 event.
    """
    portfolio_decision_id: str
    recommendation_id: str
    risk_decision_id: str
    decision_type: str
    target_weight: float
    allocated_capital: float
    rationale: str
