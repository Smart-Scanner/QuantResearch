from src.core.contracts.base import DomainCommand

class TriggerEodIngestion(DomainCommand):
    """
    Command to trigger the End of Day ingestion process.
    Contains the target date string (e.g. '2026-06-21').
    """
    target_date: str
