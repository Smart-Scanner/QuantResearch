/**
 * Phase 5.2.2: Zerodha Kite Connect Adapter — Trading Mode
 *
 * Architecture:
 *   Browser → zerodha-adapter.js → Flask Backend Proxy → kiteconnect SDK → Kite API
 *
 * Modes:
 *   read_only  — Phase 5.2.0 default. placeOrder/cancelOrder blocked.
 *   trading    — Enabled when called through BrokerHub.executeOrder() with
 *                broker ExecutionContext set. Direct calls blocked.
 *
 * Safety: 6 gates enforced by CREATE_BROKER_ORDER command handler BEFORE
 * BrokerHub.executeOrder() is ever called. adapter._assertBrokerContext() is
 * defense-in-depth only.
 *
 * 5.3 PLACEHOLDER: Risk Engine gate inserted in CREATE_BROKER_ORDER before
 * BrokerHub.executeOrder() in Phase 5.3.1. Adapter itself has no risk logic.
 */

(function() {
    'use strict';

    const PROXY_BASE = '/api/broker/zerodha';

    // ── HTTP helper ───────────────────────────────────────────────────────
    async function _fetch(endpoint, options = {}) {
        try {
            const resp = await fetch(`${PROXY_BASE}${endpoint}`, {
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', ...options.headers },
                ...options,
            });
            if (!resp.ok) {
                const text = await resp.text();
                return { success: false, error: `HTTP ${resp.status}: ${text}` };
            }
            return await resp.json();
        } catch (e) {
            return { success: false, error: e.message };
        }
    }

    // ── Kite Product + Order type constants ───────────────────────────────
    const PRODUCT = Object.freeze({ CNC: 'CNC', MIS: 'MIS', NRML: 'NRML' });
    const ORDER_TYPE = Object.freeze({ MARKET: 'MARKET', LIMIT: 'LIMIT', SL: 'SL', SL_M: 'SL-M' });
    const VALIDITY = Object.freeze({ DAY: 'DAY', IOC: 'IOC' });
    const VARIETY = Object.freeze({ REGULAR: 'regular', AMO: 'amo', CO: 'co' });

    // ── Adapter ───────────────────────────────────────────────────────────
    const ZerodhaAdapter = {
        broker_id: 'zerodha',
        broker_name: 'Zerodha Kite',
        adapter_version: '5.2.2',

        // Phase 5.2.2: trading capabilities now declared
        capabilities: Object.freeze({
            is_read_only: false,
            supports_trading: true,
            supports_market_order: true,
            supports_limit_order: true,
            supports_sl_order: true,
            supports_bracket_order: false,  // out of scope
            supports_cover_order: false,    // out of scope
            supports_gtt: false,            // out of scope
            supports_partial_fills: true,   // webhook-compatible
        }),

        // ── Internal context guard (defense-in-depth, Gap B) ─────────────
        _assertBrokerContext() {
            const ctx = (window.MarketOS && window.MarketOS.ExecutionContext)
                ? window.MarketOS.ExecutionContext.get()
                : null;
            if (ctx !== 'broker') {
                throw Object.assign(new Error('DIRECT_ADAPTER_CALL_BLOCKED'), {
                    error_code: 'DIRECT_ADAPTER_CALL_BLOCKED',
                    detail: 'adapter.placeOrder() must be called through BrokerHub.executeOrder() only'
                });
            }
        },

        // ── Connectivity ──────────────────────────────────────────────────
        connect: async function(credentials) {
            if (credentials && credentials.mode && credentials.mode.startsWith('certification')) {
                const status = await _fetch('/status');
                if (status.connected) {
                    return { success: true, session_id: `kite_${status.broker_user_id}`, error: null };
                }
                return { success: true, session_id: 'kite_cert_simulated', error: null };
            }
            const result = await _fetch('/login');
            if (result.success && result.login_url) {
                return { success: true, login_url: result.login_url, error: null };
            }
            return { success: false, error: result.error || 'Login URL generation failed' };
        },

        disconnect: async function() {
            const result = await _fetch('/disconnect', { method: 'POST' });
            return { success: result.success !== false };
        },

        getConnectionStatus: async function() {
            const result = await _fetch('/status');
            return {
                connected: result.connected || false,
                last_heartbeat: result.connected ? new Date().toISOString() : null,
                broker_user_id: result.broker_user_id || null,
                login_time: result.login_time || null,
            };
        },

        // ── Read data ─────────────────────────────────────────────────────
        getAccountInfo: async function() {
            const [profileRes, fundsRes] = await Promise.all([
                _fetch('/profile'),
                _fetch('/funds'),
            ]);
            if (!profileRes.success && !fundsRes.success) {
                return { success: false, account: null, error: profileRes.error || fundsRes.error };
            }
            const profile = profileRes.profile || {};
            const margins = fundsRes.margins || {};
            const equity = margins.equity || {};
            return {
                success: true,
                account: {
                    broker_user_id: profile.user_id || '',
                    user_name: profile.user_name || '',
                    email: profile.email || '',
                    broker: profile.broker || 'ZERODHA',
                    balance: equity.available?.live_balance || 0,
                    margin_used: equity.utilised?.debits || 0,
                    margin_available: equity.available?.adhoc_margin || 0,
                },
                error: null,
            };
        },

        getPositions: async function() {
            const result = await _fetch('/positions');
            if (!result.success) {
                return { success: false, positions: [], error: result.error };
            }
            return { success: true, positions: result.positions || [], error: null };
        },

        getOrderStatus: async function(brokerOrderId) {
            const result = await _fetch(`/orders/${brokerOrderId}`);
            if (!result.success) {
                // Fallback: scan full order list
                const all = await _fetch('/orders');
                if (!all.success) return { status: 'unknown', fills: [], error: all.error };
                const order = (all.orders || []).find(o => o.order_id === brokerOrderId);
                if (order) {
                    return {
                        status: _mapKiteStatus(order.status),
                        filled_quantity: order.filled_quantity || 0,
                        pending_quantity: order.pending_quantity || 0,
                        average_price: order.average_price || null,
                        broker_order_id: order.order_id,
                        fills: order.fills || [],
                        error: null,
                    };
                }
                return { status: 'unknown', fills: [], error: null };
            }
            const o = result.order || result;
            return {
                status: _mapKiteStatus(o.status),
                filled_quantity: o.filled_quantity || 0,
                pending_quantity: o.pending_quantity || 0,
                average_price: o.average_price || null,
                broker_order_id: o.order_id,
                fills: o.fills || [],
                error: null,
            };
        },

        // ── TRADING METHODS (Phase 5.2.2) ─────────────────────────────────

        /**
         * Place a live order via Kite Connect.
         * Must be called ONLY through BrokerHub.executeOrder() — never directly.
         * @param {object} order - BrokerOrder from BrokerHubRepository
         * @returns {{ success, broker_order_id, kite_order_id, error }}
         */
        placeOrder: async function(order) {
            // Defense-in-depth: verify broker context set by BrokerHub
            this._assertBrokerContext();

            const variety = VARIETY.REGULAR;
            const kiteOrder = {
                tradingsymbol: order.symbol,
                exchange:       order.exchange || 'NSE',
                transaction_type: order.side.toUpperCase() === 'BUY' ? 'BUY' : 'SELL',
                order_type:     _resolveOrderType(order),
                quantity:       order.quantity,
                price:          order.order_type === 'market' ? 0 : (order.price || 0),
                trigger_price:  order.trigger_price || 0,
                product:        order.product_type || PRODUCT.CNC,
                validity:       order.validity || VALIDITY.DAY,
                tag:            order.intent_id,   // FROZEN: traceability key, never change
                disclosed_quantity: 0,
            };

            const result = await _fetch(`/orders/${variety}`, {
                method: 'POST',
                body: JSON.stringify(kiteOrder),
            });

            if (!result.success || !result.order_id) {
                return {
                    success: false,
                    broker_order_id: null,
                    kite_order_id: null,
                    error: result.error || result.message || 'Order placement failed',
                };
            }

            return {
                success: true,
                broker_order_id: result.order_id,
                kite_order_id: result.order_id,
                variety: variety,
                error: null,
            };
        },

        /**
         * Cancel an open order.
         * Must be called through BrokerHub — never directly.
         */
        cancelOrder: async function(brokerOrderId, variety = 'regular') {
            this._assertBrokerContext();

            const result = await _fetch(`/orders/${variety}/${brokerOrderId}`, {
                method: 'DELETE',
            });

            return {
                success: result.success !== false,
                broker_order_id: brokerOrderId,
                error: result.error || null,
            };
        },

        /**
         * Modify an existing open order.
         * Must be called through BrokerHub — never directly.
         */
        modifyOrder: async function(brokerOrderId, updates, variety = 'regular') {
            this._assertBrokerContext();

            const result = await _fetch(`/orders/${variety}/${brokerOrderId}`, {
                method: 'PUT',
                body: JSON.stringify(updates),
            });

            return {
                success: result.success !== false,
                broker_order_id: brokerOrderId,
                error: result.error || null,
            };
        },
    };

    // ── Internal helpers ──────────────────────────────────────────────────
    function _resolveOrderType(order) {
        const t = (order.order_type || 'market').toLowerCase();
        if (t === 'limit')  return ORDER_TYPE.LIMIT;
        if (t === 'sl')     return ORDER_TYPE.SL;
        if (t === 'sl-m')   return ORDER_TYPE.SL_M;
        return ORDER_TYPE.MARKET;
    }

    function _mapKiteStatus(kiteStatus) {
        const map = {
            'OPEN':            'open',
            'COMPLETE':        'filled',
            'CANCELLED':       'cancelled',
            'REJECTED':        'rejected',
            'PENDING':         'pending',
            'TRIGGER PENDING': 'pending',
        };
        return map[(kiteStatus || '').toUpperCase()] || 'unknown';
    }

    // ── Registration ──────────────────────────────────────────────────────
    if (window.MarketOS) {
        if (window.MarketOS._brokerAdapters) {
            window.MarketOS._brokerAdapters['zerodha'] = ZerodhaAdapter;
        }
    }
    window.ZerodhaAdapter = ZerodhaAdapter;

})();
