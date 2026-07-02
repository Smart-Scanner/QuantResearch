from abc import ABC, abstractmethod
from typing import Dict, Any
from src.domains.portfolio.models.snapshot import PortfolioDecisionPayload

class PortfolioEngine(ABC):
    """
    Base contract for converting Risk Decisions into Capital Allocation instructions.
    """
    @abstractmethod
    def allocate(self, risk_decision_payload: dict, recommendation_payload: dict, portfolio_snapshot: dict) -> PortfolioDecisionPayload:
        """
        Determines the target weight for a recommendation based on conviction and risk approval.
        """
        pass
