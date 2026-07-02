from pydantic import BaseModel

class RiskDecisionIssuedPayload(BaseModel):
    """
    Payload for the RiskDecisionIssued v1 event.
    """
    risk_decision_id: str
    recommendation_id: str
    decision_state: str
    reason: str
