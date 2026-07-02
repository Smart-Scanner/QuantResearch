# Route / Template / API Dependency Inventory

This document tracks the inventory of HTML pages, routes, API endpoints, and template files in the MarketOS / Smart Screener codebase as of the Phase 5.4 Architecture Freeze.

## 1. Web Page Routes & Templates

| Route | Template File | Owner Domain | Status | Notes |
|---|---|---|---|---|
| `/` | `templates/landing.html` (logged out) | Pages | **ACTIVE** | Public marketing page |
| `/` | `templates/v3/dashboard.html` (logged in) | Pages | **ACTIVE** | Main V3 operator dashboard |
| `/v2` | `templates/index.html` | Pages | **LEGACY** | Legacy V2 dashboard (keep 2-4 weeks) |
| `/top-picks` | `templates/v3/top_picks.html` | Pages | **ACTIVE** | Top picks watchlist & alerts |
| `/golden` | `templates/v3/golden.html` | Pages | **ACTIVE** | Golden crossover breakout screen |
| `/hc` | `templates/v3/high_conviction.html` | Pages | **ACTIVE** | High conviction research lists |
| `/breakouts` | `templates/v3/breakouts.html` | Pages | **ACTIVE** | Momentum breakout feed |
| `/discovery` | `templates/discovery_center.html` | Pages | **ACTIVE** | Watchlist scanner and candidate discovery |
| `/research` | `templates/research_center.html` | Pages | **ACTIVE** | Detail research evaluation hub |
| `/mission-control` | `templates/mission_control.html` | Pages | **ACTIVE** | System health, circuit breakers & reconciliation NOC |
| `/market` | `templates/v3/market_intel.html` | Pages | **ACTIVE** | General market news & indices |
| `/paper-trades-view` | `templates/v3/paper_trades.html` | Pages | **ACTIVE** | Current paper trading entries & exits |
| `/outcome` | `templates/v3/outcome.html` | Pages | **ACTIVE** | Trade statistics & performance review |
| `/watchlist` | `templates/v3/watchlist.html` | Pages | **ACTIVE** | Custom watchlists |
| `/settings` | `templates/v3/settings.html` | Pages | **ACTIVE** | User/system configurations |
| `/pricing` | `templates/pricing.html` | Pages | **ACTIVE** | Payment plans details |
| `/about` | `templates/about.html` | Pages | **ACTIVE** | Company info |
| `/contact` | `templates/contact.html` | Pages | **ACTIVE** | Contact form |
| `/symbol/<symbol>` | `templates/symbol_workspace.html` | Pages | **ACTIVE** | Canonical symbol analysis workspace |
| `/stock/<symbol>` | *None* | Pages | **DEPRECATED** | Redirects with 301 to `/symbol/<symbol>` |
| `/portfolio` | `templates/portfolio_center.html` | Pages | **ACTIVE** | Portfolio manager |
| `/subscribe` | `templates/subscribe.html` | Pages | **ACTIVE** | Payment/Trial expired wall page |
| `/admin` | `templates/admin_dashboard.html` | Admin | **ACTIVE** | Main administrative panel |
| `/auth/login` / `/auth/local-login` | `templates/local_login.html` | Auth | **ACTIVE** | Local user authentication page |

---

## 2. Unreferenced / Orphaned Templates (Audit Candidates)

These files are present in the directory but are not registered or rendered by any Python routes:

| Template File | Status | Action / Recommendation |
|---|---|---|
| `templates/auth_error.html` | **ORPHANED** | Safe to delete. |
| `templates/index_v3_backup.html` | **ORPHANED** | Backup copy. Safe to delete. |
| `templates/index_v5_backup.html` | **ORPHANED** | Backup copy. Safe to delete. |
| `templates/portfolio.html` | **DEPRECATED** | Legacy single portfolio page. Replaced by `portfolio_center.html`. Do not delete immediately; keep in deprecated state until verified. |
| `templates/stock_detail.html` | **DEPRECATED** | Legacy single stock info page. Replaced by `symbol_workspace.html`. Keep in deprecated state until verification loop is closed. |

---

## 3. Core API Endpoints

Served under `/api` in `routes/api.py`:

| Endpoint | Method | Purpose | Owner Domain | Status |
|---|---|---|---|---|
| `/api/candidates` | GET | List scanner candidates | Discovery | **ACTIVE** |
| `/api/reconciliation/run` | POST | Trigger manual recon run | Broker | **ACTIVE** |
| `/api/reconciliation/cases` | GET | List open recon divergence cases | Broker | **ACTIVE** |
| `/api/certification/status` | GET | Query CertificationRegistry state | Governance | **ACTIVE** |
| `/api/certification/evidence` | POST | Record Domain 17 evidence | Governance | **ACTIVE** |
| `/api/shadow-sessions` | GET | List shadow trading sessions | Governance | **ACTIVE** |
| `/api/commands/dispatch` | POST | Dispatch operation commands to bus | Governance | **ACTIVE** |
