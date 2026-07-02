import random
from typing import List
from src.domains.market_data.models.snapshot import DataRecord

class MockIngestionService:
    """
    Mock service to generate deterministic known-good EOD data 
    for the MVP 50-100 symbol universe.
    """
    def __init__(self):
        # We will hardcode 50 symbols for the MVP universe.
        self.symbols = [f"STOCK_{i}" for i in range(1, 51)]
        
    def fetch_eod_data(self, target_date: str) -> List[DataRecord]:
        """
        Generates deterministic data for a specific date.
        If we call this twice with the same date, it produces the exact same records.
        """
        # Seed the random generator with the date so it is 100% deterministic
        random.seed(target_date)
        
        records = []
        for symbol in self.symbols:
            base_price = random.uniform(100.0, 500.0)
            open_p = round(base_price, 2)
            high_p = round(base_price * 1.05, 2)
            low_p = round(base_price * 0.95, 2)
            close_p = round(random.uniform(low_p, high_p), 2)
            volume = int(random.uniform(10000, 1000000))
            
            records.append(DataRecord(
                symbol=symbol,
                date=target_date,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=volume
            ))
            
        return records
