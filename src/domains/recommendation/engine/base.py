from abc import ABC, abstractmethod
from typing import List, Optional
from src.domains.recommendation.models.version import RecommendationVersionPayload

class RecommendationAggregator(ABC):
    """
    Base contract for Aggregating strategy signals into unified recommendations.
    Ensures that when 5+ strategies are added, we don't have hardcoded weighting logic in the handlers.
    """
    @abstractmethod
    def aggregate(self, symbol: str, signal_payloads: List[dict]) -> Optional[RecommendationVersionPayload]:
        """
        Takes raw signal payloads from different strategies for a single symbol
        and synthesizes a single RecommendationVersionPayload.
        """
        pass
