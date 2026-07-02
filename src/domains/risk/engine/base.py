from abc import ABC, abstractmethod
from typing import Dict, Any
from src.domains.risk.models.decision import RiskDecisionPayload

class RiskEngine(ABC):
    """
    Base contract for all Risk evaluations.
    """
    @abstractmethod
    def evaluate(self, recommendation_payload: dict, portfolio_snapshot: dict) -> RiskDecisionPayload:
        """
        Evaluates a single recommendation against the current portfolio risk state.
        """
        pass
