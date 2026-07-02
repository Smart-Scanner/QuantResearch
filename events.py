"""
Canonical Event Taxonomy — Section 49 of the Master Plan.

Single source of truth for all platform event types.
Prevents event naming drift over time.

Every emitted event MUST conform to this taxonomy.
"""

# ── Scan Lifecycle Events ─────────────────────────────────────────────
SCAN_CREATED = "SCAN_CREATED"
SCAN_STARTED = "SCAN_STARTED"
SCAN_REJECTED = "SCAN_REJECTED"          # Duplicate / lock contention
SCAN_PHASE_CHANGED = "SCAN_PHASE_CHANGED"
SCAN_COMPLETED = "SCAN_COMPLETED"
SCAN_FAILED = "SCAN_FAILED"
SCAN_CANCELLED = "SCAN_CANCELLED"
SCAN_STALE = "SCAN_STALE"               # Watchdog detected stale
SCAN_RECOVERED = "SCAN_RECOVERED"        # Watchdog recovered stale

# ── Watchdog Events ───────────────────────────────────────────────────
WATCHDOG_TRIGGERED = "WATCHDOG_TRIGGERED"
WATCHDOG_HEARTBEAT = "WATCHDOG_HEARTBEAT"
WATCHDOG_STARTED = "WATCHDOG_STARTED"
WATCHDOG_STOPPED = "WATCHDOG_STOPPED"

# ── System Events ─────────────────────────────────────────────────────
GRACEFUL_SHUTDOWN = "GRACEFUL_SHUTDOWN"
CONFIG_DRIFT_DETECTED = "CONFIG_DRIFT_DETECTED"

# ── State Transition Actors ───────────────────────────────────────────
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"
ACTOR_WATCHDOG = "watchdog"
ACTOR_AUTO_SCAN = "auto_scan"
ACTOR_API = "api"


# ── Event Payload Schema (required fields per event type) ─────────────
_REQUIRED_FIELDS = {
    SCAN_CREATED:       {"scan_id", "correlation_id", "trigger_source"},
    SCAN_STARTED:       {"scan_id", "correlation_id"},
    SCAN_REJECTED:      {"scan_id", "reason"},
    SCAN_PHASE_CHANGED: {"scan_id", "old_phase", "new_phase"},
    SCAN_COMPLETED:     {"scan_id", "correlation_id", "duration_seconds"},
    SCAN_FAILED:        {"scan_id", "correlation_id", "error_message"},
    SCAN_CANCELLED:     {"scan_id", "correlation_id", "actor"},
    SCAN_STALE:         {"scan_id", "age_minutes"},
    SCAN_RECOVERED:     {"scan_id", "recovery_action"},
    WATCHDOG_TRIGGERED: {"scan_id", "age_minutes"},
    WATCHDOG_HEARTBEAT: {"timestamp"},
    GRACEFUL_SHUTDOWN:  {"reason"},
}


def validate_event(event_type: str, payload: dict) -> tuple[bool, list[str]]:
    """Validate an event payload against the canonical schema.
    Returns (is_valid, list_of_missing_fields).
    """
    required = _REQUIRED_FIELDS.get(event_type)
    if required is None:
        return True, []  # Unknown event types pass (forward-compatible)
    missing = [f for f in required if f not in payload]
    return len(missing) == 0, missing
