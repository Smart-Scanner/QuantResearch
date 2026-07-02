# MarketOS Event Catalog v1.0

This document serves as the formal architecture freeze for **Event Catalog v1**. It details all event schemas produced and consumed within the MarketOS event-driven framework, establishing clear domain boundaries and contracts.

---

## Universal Event Envelope Schema

Every event published via the `createEvent` API conforms to the following universal envelope format:

```typescript
interface MarketOSEvent {
  event_id: string;        // UUIDv4 unique event instance identifier
  event_type: string;      // Event type name (must be in the catalog)
  event_version: string;   // Envelope format version (always "1.0")
  entity_type: string;     // Target entity type (Asset, Position, Research, etc.)
  entity_id: string;       // ID of the target entity
  user_id: string;         // Operator ID (e.g., "admin" or sub)
  timestamp: string;       // ISO 8601 UTC timestamp
  source: string;          // Source classification (system | manual | ai | broker | discovery | research)
  metadata: Record<string, any>; // Event-specific payload schema
}
```

---

## Domain Event Catalog

### 1. Market Data Domain
Events related to structural instruments and raw feeds.

#### `SYMBOL_MASTER_UPDATED`
```json
{
  "event_type": "SYMBOL_MASTER_UPDATED",
  "owner_domain": "market_data",
  "producer": "market_data",
  "consumers": ["discovery", "intelligence", "portfolio"],
  "required_fields": ["symbol", "exchange"],
  "optional_fields": ["sector_id", "sector_name"],
  "schema_version": "1.0"
}
```

---

### 2. Intelligence Domain
Events related to composite scoring and sector classification metrics.

#### `INTELLIGENCE_PROFILE_CREATED`
```json
{
  "event_type": "INTELLIGENCE_PROFILE_CREATED",
  "owner_domain": "intelligence",
  "producer": "intelligence",
  "consumers": ["discovery"],
  "required_fields": ["instrument_id", "scores"],
  "optional_fields": ["tags", "sector"],
  "schema_version": "1.0"
}
```

---

### 3. Discovery Domain
Events related to scans, inbox triage, and watchlists.

#### `SCAN_DEF_CREATED`
```json
{
  "event_type": "SCAN_DEF_CREATED",
  "owner_domain": "discovery",
  "producer": "discovery",
  "consumers": ["system"],
  "required_fields": ["scan_def_id", "name"],
  "optional_fields": ["filters", "version"],
  "schema_version": "1.0"
}
```

#### `SCAN_DEF_UPDATED`
```json
{
  "event_type": "SCAN_DEF_UPDATED",
  "owner_domain": "discovery",
  "producer": "discovery",
  "consumers": ["system"],
  "required_fields": ["scan_def_id", "name"],
  "optional_fields": ["filters", "version"],
  "schema_version": "1.0"
}
```

#### `SCAN_COMPLETED`
```json
{
  "event_type": "SCAN_COMPLETED",
  "owner_domain": "discovery",
  "producer": "discovery",
  "consumers": ["discovery"],
  "required_fields": ["scan_id", "scan_type", "result_count"],
  "optional_fields": ["result_hash"],
  "schema_version": "1.0"
}
```

#### `WATCHLIST_CREATED`
```json
{
  "event_type": "WATCHLIST_CREATED",
  "owner_domain": "discovery",
  "producer": "discovery",
  "consumers": ["system"],
  "required_fields": ["watchlist_name"],
  "optional_fields": ["description"],
  "schema_version": "1.0"
}
```

#### `WATCHLIST_UPDATED`
```json
{
  "event_type": "WATCHLIST_UPDATED",
  "owner_domain": "discovery",
  "producer": "discovery",
  "consumers": ["system"],
  "required_fields": ["watchlist_name"],
  "optional_fields": ["description", "symbol_count"],
  "schema_version": "1.0"
}
```

#### `WATCHLIST_ADDED`
```json
{
  "event_type": "WATCHLIST_ADDED",
  "owner_domain": "discovery",
  "producer": "discovery",
  "consumers": ["system"],
  "required_fields": ["watchlist_name", "symbol"],
  "optional_fields": ["origin"],
  "schema_version": "1.0"
}
```

---

### 4. Research Domain
Events bridging candidate triage and thesis verification.

#### `CANDIDATE_CREATED`
```json
{
  "event_type": "CANDIDATE_CREATED",
  "owner_domain": "research",
  "producer": "research",
  "consumers": ["research"],
  "required_fields": ["candidate_id", "instrument_id", "priority_score"],
  "optional_fields": ["provenance"],
  "schema_version": "1.0"
}
```

#### `CANDIDATE_UPDATED`
```json
{
  "event_type": "CANDIDATE_UPDATED",
  "owner_domain": "research",
  "producer": "research",
  "consumers": ["research"],
  "required_fields": ["candidate_id", "new_status"],
  "optional_fields": ["snapshot_id"],
  "schema_version": "1.0"
}
```

#### `RESEARCH_CREATED`
```json
{
  "event_type": "RESEARCH_CREATED",
  "owner_domain": "research",
  "producer": "research",
  "consumers": ["portfolio", "analytics"],
  "required_fields": ["snapshot_id", "instrument_id", "snapshot_group_id", "version"],
  "optional_fields": ["parent_snapshot_id", "risk_reward", "targets", "provenance_type"],
  "schema_version": "1.0"
}
```

#### `RESEARCH_APPROVED`
```json
{
  "event_type": "RESEARCH_APPROVED",
  "owner_domain": "research",
  "producer": "research",
  "consumers": ["portfolio", "analytics"],
  "required_fields": ["snapshot_id", "instrument_id"],
  "optional_fields": ["approved_by"],
  "schema_version": "1.0"
}
```

---

### 5. Portfolio Domain
Events related to trade execution management and reflection.

#### `POSITION_OPENED`
```json
{
  "event_type": "POSITION_OPENED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["execution", "analytics", "review"],
  "required_fields": ["position_id", "portfolio_id", "symbol", "entry_price", "quantity"],
  "optional_fields": ["strategy", "side", "snapshot_id"],
  "schema_version": "1.0"
}
```

#### `POSITION_MODIFIED`
```json
{
  "event_type": "POSITION_MODIFIED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["analytics"],
  "required_fields": ["position_id"],
  "optional_fields": ["changes"],
  "schema_version": "1.0"
}
```

#### `POSITION_SCALED`
```json
{
  "event_type": "POSITION_SCALED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["analytics"],
  "required_fields": ["position_id", "scale_type", "price", "quantity"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `STOP_UPDATED`
```json
{
  "event_type": "STOP_UPDATED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["execution", "alerts"],
  "required_fields": ["position_id", "old_stop", "new_stop"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `TARGET_UPDATED`
```json
{
  "event_type": "TARGET_UPDATED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["execution", "alerts"],
  "required_fields": ["position_id", "old_target", "new_target"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `PARTIAL_EXIT`
```json
{
  "event_type": "PARTIAL_EXIT",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["execution", "analytics"],
  "required_fields": ["position_id", "price", "quantity"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `POSITION_CLOSED`
```json
{
  "event_type": "POSITION_CLOSED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["review", "analytics"],
  "required_fields": ["position_id", "exit_price", "realized_pnl"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `TARGET_HIT`
```json
{
  "event_type": "TARGET_HIT",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["execution", "analytics"],
  "required_fields": ["position_id", "price_hit"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `STOP_HIT`
```json
{
  "event_type": "STOP_HIT",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["execution", "analytics"],
  "required_fields": ["position_id", "price_hit"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `REVIEW_CREATED`
```json
{
  "event_type": "REVIEW_CREATED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["analytics"],
  "required_fields": ["review_id", "position_id"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `REVIEW_UPDATED`
```json
{
  "event_type": "REVIEW_UPDATED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["analytics"],
  "required_fields": ["review_id", "status"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `REVIEW_COMPLETED`
```json
{
  "event_type": "REVIEW_COMPLETED",
  "owner_domain": "portfolio",
  "producer": "portfolio",
  "consumers": ["analytics"],
  "required_fields": ["review_id"],
  "optional_fields": ["completed_at"],
  "schema_version": "1.0"
}
```

---

### 6. Execution & Alert Domains
External command-handling and alerting.

#### `ORDER_FILLED`
```json
{
  "event_type": "ORDER_FILLED",
  "owner_domain": "execution",
  "producer": "execution",
  "consumers": ["portfolio"],
  "required_fields": ["order_id", "symbol", "price", "quantity", "side", "status"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `ORDER_CANCELLED`
```json
{
  "event_type": "ORDER_CANCELLED",
  "owner_domain": "execution",
  "producer": "execution",
  "consumers": ["portfolio"],
  "required_fields": ["order_id", "reason"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `ALERT_CREATED`
```json
{
  "event_type": "ALERT_CREATED",
  "owner_domain": "alerts",
  "producer": "alerts",
  "consumers": ["system"],
  "required_fields": ["alert_id", "symbol", "condition"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```

#### `ALERT_TRIGGERED`
```json
{
  "event_type": "ALERT_TRIGGERED",
  "owner_domain": "alerts",
  "producer": "alerts",
  "consumers": ["discovery"],
  "required_fields": ["alert_id", "symbol", "condition"],
  "optional_fields": ["value"],
  "schema_version": "1.0"
}
```

#### `JOURNAL_ENTRY`
```json
{
  "event_type": "JOURNAL_ENTRY",
  "owner_domain": "analytics",
  "producer": "analytics",
  "consumers": ["review"],
  "required_fields": ["journal_id", "position_id", "text"],
  "optional_fields": [],
  "schema_version": "1.0"
}
```
