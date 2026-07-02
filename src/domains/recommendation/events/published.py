from typing import List
from pydantic import BaseModel

class RecommendationPublishedPayload(BaseModel):
    """
    Payload for the RecommendationPublished v1 event.
    """
    recommendation_id: str
    symbol: str
    recommendation_state: str
    conviction: str
    parent_strategy_signal_ids: List[str]
