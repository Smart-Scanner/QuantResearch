from typing import List, Dict, Any

class UniverseFilterService:
    """
    Applies deterministic rules to filter the DataSnapshot into a Universe.
    Rules: Volume > 50000 and Close > 50.
    """
    def filter_eligible_symbols(self, data_records: List[Dict[str, Any]]) -> List[str]:
        eligible = []
        for record in data_records:
            if record.get("volume", 0) > 50000 and record.get("close", 0) > 50:
                eligible.append(record["symbol"])
        return sorted(eligible)
