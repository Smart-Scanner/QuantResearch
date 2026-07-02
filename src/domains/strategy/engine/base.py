from abc import ABC, abstractmethod
from typing import List, Any
from src.domains.strategy.models.signal import StrategySignal

class StrategyEngine(ABC):
    """
    Base contract for all Alpha generation logic.
    Ensures we don't build a giant 'if strategy == X' monolith.
    """
    @abstractmethod
    def evaluate(self, research_snapshot_payload: dict, correlation_id: str, causation_id: str) -> List[StrategySignal]:
        """
        Consumes objective Research Facts and produces Strategy Signals.
        """
        pass
