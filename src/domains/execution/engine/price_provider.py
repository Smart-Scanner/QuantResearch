from abc import ABC, abstractmethod
from decimal import Decimal

class PriceProvider(ABC):
    """
    Contract for obtaining current market price to calculate quantity.
    Execution domain must never know WHERE the price comes from.
    """
    @abstractmethod
    async def get_price(self, symbol: str) -> Decimal:
        pass

class StaticPriceProvider(PriceProvider):
    """
    MVP Price Provider. Returns a static price.
    """
    async def get_price(self, symbol: str) -> Decimal:
        return Decimal("500.0")
