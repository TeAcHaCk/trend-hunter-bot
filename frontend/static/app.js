/**
 * Trend Hunter Futures Bot — Frontend Application Logic
 * Handles WebSocket connections, API calls, UI updates, and animations.
 */

// ═══════════════════════════════════════════════════════
// CONFIGURATION
// ═══════════════════════════════════════════════════════

const API_BASE = '';
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws/live`;

// ═══════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════

const state = {
    botStatus: 'STOPPED',
    wsConnected: false,
    prices: {},
    positions: {},
    strategies: {},
    testnet: true,
    lastUpdate: null,
    lastWsAt: null,   // performance.now() of last WS message
};

let ws = null;
let reconnectTimer = null;
let statusPollTimer = null;

// Activity log state
const MAX_LOG_ENTRIES = 50;
let _prevBotStatus = null;
let _prevSignalCount = 0;
let _prevTradeCount = 0;
let _prevOrdersPlaced = {}; // symbol -> bool

// ═══════════════════════════════════════════════════════
// API HELPERS
// ═══════════════════════════════════════════════════════

async function api(endpoint, options = {}) {
    const { quiet, ...fetchOpts } = options;
    try {
        const response = await fetch(`${API_BASE}${endpoint}`, {
            headers: { 'Content-Type': 'application/json' },
            ...fetchOpts,
        });
        const data = await response.json();
        if (!response.ok && !quiet) {
            const msg = data?.error || data?.detail || `HTTP ${response.status}`;
            showToast(`Request failed: ${msg}`, 'error');
        } else if (data && data.success === false && !quiet) {
            const msg = data.error || data.message || 'Request failed';
            showToast(typeof msg === 'string' ? msg : JSON.stringify(msg), 'error');
        }
        return data;
    } catch (error) {
        console.error(`API error [${endpoint}]:`, error);
        if (!quiet) showToast(`Network error: ${error.message}`, 'error');
        return { success: false, error: error.message };
    }
}

async function apiGet(endpoint, options = {}) {
    return api(endpoint, options);
}

async function apiPost(endpoint, body = {}, options = {}) {
    return api(endpoint, {
        method: 'POST',
        body: JSON.stringify(body),
        ...options,
    });
}

// ═══════════════════════════════════════════════════════
// WEBSOCKET
// ═══════════════════════════════════════════════════════

function connectWebSocket() {
    if (ws && ws.readyState <= 1) return;

    try {
        ws = new WebSocket(WS_URL);

        ws.onopen = () => {
            console.log('WebSocket connected');
            state.wsConnected = true;
            updateConnectionDot(true);
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'price_update') {
                    handlePriceUpdate(msg.data);
                } else if (msg.type === 'status_update') {
                    handleStatusUpdate(msg.data);
                }
            } catch (e) {
                console.debug('WS parse error:', e);
            }
        };

        ws.onclose = () => {
            state.wsConnected = false;
            updateConnectionDot(false);
            scheduleReconnect();
        };

        ws.onerror = (err) => {
            console.debug('WebSocket error:', err);
        };
    } catch (e) {
        scheduleReconnect();
    }
}

function scheduleReconnect() {
    if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connectWebSocket();
        }, 3000);
    }
}

function updateConnectionDot(connected) {
    const dot = document.getElementById('connection-dot');
    if (dot) {
        dot.className = `connection-dot ${connected ? 'connected' : 'disconnected'}`;
    }
}

// Updates the "last update" pill once per second so the user can see at a
// glance whether the feed is live. Stale (> 5 s) → amber. Disconnected → red.
function updateLastUpdatePill() {
    const pill = document.getElementById('last-update-pill');
    if (!pill) return;
    if (!state.wsConnected) {
        pill.textContent = 'offline';
        pill.className = 'last-update-pill stale';
        return;
    }
    if (state.lastWsAt == null) {
        pill.textContent = '...';
        pill.className = 'last-update-pill';
        return;
    }
    const ageMs = performance.now() - state.lastWsAt;
    pill.textContent = formatAgo(ageMs);
    pill.className = `last-update-pill ${ageMs > 5000 ? 'stale' : ''}`;
}

// ═══════════════════════════════════════════════════════
// DATA HANDLERS
// ═══════════════════════════════════════════════════════

function handlePriceUpdate(data) {
    if (!data || !data.symbol) return;
    const prev = state.prices[data.symbol]?.price || data.price;
    state.prices[data.symbol] = data;
    state.lastWsAt = performance.now();
    updatePriceDisplay(data.symbol, data, prev);
}

function handleStatusUpdate(data) {
    if (!data) return;

    const newStatus = data.state || 'STOPPED';

    // Detect bot state changes for the activity log
    if (_prevBotStatus !== null && _prevBotStatus !== newStatus) {
        const icons = { RUNNING: '▶', STOPPED: '⏹', PAUSED: '⏸' };
        appendActivityLog(`Bot ${newStatus.toLowerCase()}`, 'system', icons[newStatus] || '🤖');
    }
    _prevBotStatus = newStatus;

    state.botStatus = newStatus;
    state.strategies = data.strategies || {};
    state.positions = data.position_manager?.positions || {};
    state.testnet = data.testnet ?? true;
    state.lastUpdate = new Date().toISOString();
    state.lastWsAt = performance.now();

    // Detect new signals
    const newSignals = data.daily_signals ?? data.total_signals ?? 0;
    if (newSignals > _prevSignalCount && _prevSignalCount >= 0) {
        const diff = newSignals - _prevSignalCount;
        appendActivityLog(`${diff} new breakout signal${diff > 1 ? 's' : ''} detected`, 'signal', '⚡');
    }
    _prevSignalCount = newSignals;

    // Detect new trades
    const newTrades = data.daily_trades ?? data.total_trades ?? 0;
    if (newTrades > _prevTradeCount && _prevTradeCount >= 0) {
        const diff = newTrades - _prevTradeCount;
        appendActivityLog(`${diff} new trade${diff > 1 ? 's' : ''} executed`, 'trade', '🔄');
    }
    _prevTradeCount = newTrades;

    // Detect order placement per symbol
    if (data.strategies) {
        Object.entries(data.strategies).forEach(([symbol, strat]) => {
            const wasPlaced = _prevOrdersPlaced[symbol] || false;
            if (strat.orders_placed && !wasPlaced) {
                appendActivityLog(`${symbol} limit orders placed at breakout levels`, 'signal', '⏳');
            } else if (!strat.orders_placed && wasPlaced && !strat.in_trade) {
                appendActivityLog(`${symbol} pending orders expired / cancelled`, 'system', '🗑️');
            }
            if (strat.in_trade && !wasPlaced) {
                const dir = strat.trade_direction || 'UNKNOWN';
                appendActivityLog(`${symbol} ${dir} trade opened`, 'trade', dir === 'LONG' ? '📈' : '📉');
            }
            _prevOrdersPlaced[symbol] = strat.orders_placed || strat.in_trade || false;
        });
    }

    if (data.prices) {
        Object.entries(data.prices).forEach(([symbol, priceData]) => {
            const prev = state.prices[symbol]?.price || priceData.price;
            state.prices[symbol] = priceData;
            updatePriceDisplay(symbol, priceData, prev);
        });
    }

    updateBotStatusUI();
    updateStrategiesUI();
    updatePositionsUI();
    updateStatsUI(data);
    updateKpiStrip(data);
}

// ═══════════════════════════════════════════════════════
// UI UPDATES — Dashboard
// ═══════════════════════════════════════════════════════

function updatePriceDisplay(symbol, data, prevPrice) {
    const priceEl = document.getElementById(`price-${symbol}`);
    if (!priceEl) return;

    const price = data.close || data.price || data.mark_price || 0;
    const direction = price > prevPrice ? 'up' : price < prevPrice ? 'down' : '';

    // Animate price change & drop the loading shimmer once a real value lands
    priceEl.textContent = formatPrice(price, symbol);
    priceEl.classList.remove('skeleton');
    priceEl.className = `price-value ${direction}`;

    // Flash effect
    if (direction) {
        priceEl.style.transition = 'none';
        priceEl.offsetHeight; // force reflow
        priceEl.style.transition = 'color 0.5s ease';
        setTimeout(() => {
            priceEl.className = 'price-value';
        }, 800);
    }

    // Update high/low
    const highEl = document.getElementById(`high-${symbol}`);
    const lowEl = document.getElementById(`low-${symbol}`);
    if (highEl && data.high) highEl.textContent = formatPrice(data.high, symbol);
    if (lowEl && data.low) lowEl.textContent = formatPrice(data.low, symbol);

    // Update breakout bar
    updateBreakoutBar(symbol, price, data.low, data.high);

    // Update volume
    const volEl = document.getElementById(`vol-${symbol}`);
    if (volEl && data.volume) volEl.textContent = formatVolume(data.volume);
}

function updateBreakoutBar(symbol, price, low, high) {
    const marker = document.getElementById(`marker-${symbol}`);
    if (!marker || !low || !high || high <= low) return;

    const range = high - low;
    const pct = Math.max(0, Math.min(100, ((price - low) / range) * 100));
    marker.style.left = `${pct}%`;

    // Color the fill bar
    const fill = document.getElementById(`fill-${symbol}`);
    if (fill) {
        fill.style.width = `${pct}%`;
        if (pct > 90) {
            fill.style.background = 'var(--gradient-green)';
        } else if (pct < 10) {
            fill.style.background = 'var(--gradient-red)';
        } else {
            fill.style.background = 'var(--gradient-primary)';
        }
    }
}

function updateBotStatusUI() {
    const badge = document.getElementById('bot-status-badge');
    if (!badge) return;

    const statusClass = state.botStatus.toLowerCase();
    badge.className = `status-badge ${statusClass}`;
    badge.innerHTML = `<span class="status-dot"></span>${state.botStatus}`;

    // Update control buttons
    const startBtn = document.getElementById('btn-start');
    const stopBtn = document.getElementById('btn-stop');
    const pauseBtn = document.getElementById('btn-pause');

    if (startBtn) startBtn.disabled = state.botStatus === 'RUNNING';
    if (stopBtn) stopBtn.disabled = state.botStatus === 'STOPPED';
    if (pauseBtn) {
        pauseBtn.disabled = state.botStatus === 'STOPPED';
        pauseBtn.textContent = state.botStatus === 'PAUSED' ? '▶ Resume' : '⏸ Pause';
    }

    // Testnet badge
    const testnetBadge = document.getElementById('testnet-badge');
    if (testnetBadge) {
        testnetBadge.style.display = state.testnet ? 'inline' : 'none';
    }
}

function updateStrategiesUI() {
    Object.entries(state.strategies).forEach(([symbol, strat]) => {
        const posEl = document.getElementById(`position-${symbol}`);
        if (posEl) {
            posEl.textContent = strat.current_position || 'None';
            posEl.className = strat.current_position === 'LONG'
                ? 'td-long'
                : strat.current_position === 'SHORT'
                    ? 'td-short'
                    : '';
        }

        // Breakout levels
        const bufHigh = document.getElementById(`buf-high-${symbol}`);
        const bufLow = document.getElementById(`buf-low-${symbol}`);
        if (bufHigh && strat.breakout_high) bufHigh.textContent = formatPrice(strat.breakout_high, symbol);
        if (bufLow && strat.breakout_low) bufLow.textContent = formatPrice(strat.breakout_low, symbol);

        // Range high/low on bar
        const highEl = document.getElementById(`high-${symbol}`);
        const lowEl = document.getElementById(`low-${symbol}`);
        if (highEl && strat.range_high) highEl.textContent = formatPrice(strat.range_high, symbol);
        if (lowEl && strat.range_low) lowEl.textContent = formatPrice(strat.range_low, symbol);

        // Update breakout bar with strategy range
        const price = strat.last_price || 0;
        if (price && strat.range_low && strat.range_high) {
            updateBreakoutBar(symbol, price, strat.range_low, strat.range_high);
        }

        // If WS price cache is empty, use strategy's last_price for the main display
        if (price && !state.prices[symbol]) {
            const priceEl = document.getElementById(`price-${symbol}`);
            if (priceEl) {
                priceEl.textContent = formatPrice(price, symbol);
                priceEl.classList.remove('skeleton');
                priceEl.className = 'price-value';
            }
        }

        // Trade status panel
        const tradePanel = document.getElementById(`trade-status-${symbol}`);
        const orderPanel = document.getElementById(`order-status-${symbol}`);

        if (strat.in_trade) {
            // Show trade panel, hide order panel
            if (tradePanel) tradePanel.style.display = 'block';
            if (orderPanel) orderPanel.style.display = 'none';

            if (tradePanel) {
                const dirEl = document.getElementById(`trade-dir-${symbol}`);
                if (dirEl) {
                    dirEl.textContent = `${strat.trade_direction === 'LONG' ? '↑' : '↓'} ${strat.trade_direction}`;
                    dirEl.className = `trade-direction ${(strat.trade_direction || '').toLowerCase()}`;
                }
                const slEl = document.getElementById(`trade-sl-${symbol}`);
                const tpEl = document.getElementById(`trade-tp-${symbol}`);
                const entryEl = document.getElementById(`trade-entry-${symbol}`);
                if (slEl && strat.stop_loss) slEl.textContent = formatPrice(strat.stop_loss, symbol);
                if (tpEl && strat.take_profit) tpEl.textContent = formatPrice(strat.take_profit, symbol);
                if (entryEl && strat.entry_price) entryEl.textContent = formatPrice(strat.entry_price, symbol);

                // Unrealized PnL (using contract_value for accurate USD PnL)
                const pnlEl = document.getElementById(`trade-pnl-${symbol}`);
                if (pnlEl && strat.entry_price && price) {
                    const cv = strat.contract_value || (symbol.startsWith('BTC') ? 0.001 : 0.01);
                    let pnl = 0;
                    if (strat.trade_direction === 'LONG') pnl = price - strat.entry_price;
                    else pnl = strat.entry_price - price;
                    pnl *= (strat.quantity || 1) * cv;
                    pnlEl.textContent = formatPnL(pnl);
                    pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
                }
            }
        } else if (strat.orders_placed) {
            // Show order panel, hide trade panel
            if (tradePanel) tradePanel.style.display = 'none';
            if (orderPanel) {
                orderPanel.style.display = 'block';

                // Buy order
                const buyPrice = document.getElementById(`order-buy-price-${symbol}`);
                const buyId = document.getElementById(`order-buy-id-${symbol}`);
                if (buyPrice) {
                    if (strat.long_order_id) {
                        buyPrice.textContent = formatPrice(strat.breakout_high, symbol);
                    } else {
                        buyPrice.textContent = '—';
                    }
                }
                if (buyId) {
                    buyId.textContent = strat.long_order_id ? `#${strat.long_order_id}` : 'Filtered';
                    buyId.style.opacity = strat.long_order_id ? '1' : '0.4';
                }

                // Sell order
                const sellPrice = document.getElementById(`order-sell-price-${symbol}`);
                const sellId = document.getElementById(`order-sell-id-${symbol}`);
                if (sellPrice) {
                    if (strat.short_order_id) {
                        sellPrice.textContent = formatPrice(strat.breakout_low, symbol);
                    } else {
                        sellPrice.textContent = '—';
                    }
                }
                if (sellId) {
                    sellId.textContent = strat.short_order_id ? `#${strat.short_order_id}` : 'Filtered';
                    sellId.style.opacity = strat.short_order_id ? '1' : '0.4';
                }

                // Drift-free countdown — derive remaining from server timestamp
                const timerEl = document.getElementById(`order-timer-${symbol}`);
                if (timerEl) {
                    const placedAt = strat.orders_placed_at;             // unix seconds
                    const expiry = strat.order_expiry_seconds || 600;
                    if (placedAt) {
                        const remainingSec = Math.max(
                            0,
                            Math.floor(placedAt + expiry - Date.now() / 1000),
                        );
                        const mins = Math.floor(remainingSec / 60);
                        const secs = remainingSec % 60;
                        timerEl.textContent =
                            `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
                        timerEl.style.color = remainingSec < 120
                            ? 'var(--red)'
                            : remainingSec < 300 ? 'var(--amber)' : '';

                        // Auto-hide panel when timer hits 0 (server will cancel)
                        if (remainingSec <= 0) {
                            orderPanel.style.display = 'none';
                        }
                    } else {
                        timerEl.textContent = '--:--';
                    }
                }
            }
        } else {
            // No trade, no orders — immediately hide both panels
            if (tradePanel) tradePanel.style.display = 'none';
            if (orderPanel) orderPanel.style.display = 'none';
            // Reset timer tracking
            if (window._orderStartTime && window._orderStartTime[symbol]) {
                delete window._orderStartTime[symbol];
            }
        }
    });
}

function updatePositionsUI() {
    const container = document.getElementById('positions-container');
    if (!container) return;

    const positions = Object.entries(state.positions);

    if (positions.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📊</div>
                <div class="empty-text">No active positions</div>
            </div>`;
        return;
    }

    container.innerHTML = positions.map(([symbol, pos]) => `
        <div class="position-card">
            <div>
                <span class="pos-symbol">${symbol}</span>
                <span class="pos-direction ${pos.side?.toLowerCase()}">${pos.side}</span>
            </div>
            <div style="text-align: right;">
                <div style="font-size: 0.78rem; color: var(--text-muted);">Entry: ${formatPrice(pos.entry_price, symbol)}</div>
                <div style="font-size: 0.78rem; color: var(--text-muted);">Qty: ${pos.size}</div>
            </div>
        </div>
    `).join('');
}

function updateStatsUI(data) {
    const el = (id, val) => {
        const e = document.getElementById(id);
        if (e) e.textContent = val;
    };

    // Use daily counters if available, fall back to totals for backwards compat
    el('stat-signals', data.daily_signals ?? data.total_signals ?? 0);
    el('stat-trades', data.daily_trades ?? data.total_trades ?? 0);

    const pnl = data.position_manager?.daily_pnl || 0;
    const pnlEl = document.getElementById('stat-daily-pnl');
    if (pnlEl) {
        pnlEl.textContent = formatPnL(pnl);
        pnlEl.style.color = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : '';
    }

    if (data.last_check_time) {
        el('stat-last-check', new Date(data.last_check_time).toLocaleTimeString());
    }
}

// ═══════════════════════════════════════════════════════
// BOT CONTROLS
// ═══════════════════════════════════════════════════════

async function startBot() {
    const verb = state.testnet ? 'start the bot on TESTNET' : 'start the bot on MAINNET';
    if (!await confirmAction('Start bot?',
            `This will ${verb} and begin placing orders. Continue?`)) return;

    const btn = document.getElementById('btn-start');
    if (btn) btn.disabled = true;

    const result = await apiPost('/api/bot/start', {}, { quiet: true });
    if (result.success) {
        showToast('Bot started', 'success');
    } else {
        showToast(`Failed to start bot: ${result.error || 'Unknown error'}`, 'error');
        if (btn) btn.disabled = false;
    }
}

async function stopBot() {
    if (!await confirmAction('Stop bot?',
            'This cancels pending orders and exits any open positions on next cycle.')) return;
    const result = await apiPost('/api/bot/stop', {}, { quiet: true });
    if (result.success) showToast('Bot stopped', 'warning');
    else showToast(`Failed to stop: ${result.error}`, 'error');
}

async function pauseBot() {
    const result = await apiPost('/api/bot/pause', {}, { quiet: true });
    if (result.success) showToast(result.message, 'info');
    else showToast(`Failed: ${result.error}`, 'error');
}

// Lightweight confirmation modal backed by a <dialog> in index.html.
// Falls back to window.confirm() if the dialog isn't present (testing/dev).
function confirmAction(title, body) {
    return new Promise((resolve) => {
        const dialog = document.getElementById('confirm-modal');
        if (!dialog || typeof dialog.showModal !== 'function') {
            resolve(window.confirm(`${title}\n\n${body}`));
            return;
        }
        const titleEl = dialog.querySelector('[data-confirm-title]');
        const bodyEl = dialog.querySelector('[data-confirm-body]');
        const okBtn = dialog.querySelector('[data-confirm-ok]');
        const cancelBtn = dialog.querySelector('[data-confirm-cancel]');
        if (titleEl) titleEl.textContent = title;
        if (bodyEl) bodyEl.textContent = body;

        const cleanup = (val) => {
            okBtn?.removeEventListener('click', onOk);
            cancelBtn?.removeEventListener('click', onCancel);
            dialog.removeEventListener('cancel', onCancel);
            try { dialog.close(); } catch (_) {}
            resolve(val);
        };
        const onOk = () => cleanup(true);
        const onCancel = (e) => { e?.preventDefault?.(); cleanup(false); };
        okBtn?.addEventListener('click', onOk);
        cancelBtn?.addEventListener('click', onCancel);
        dialog.addEventListener('cancel', onCancel);
        dialog.showModal();
        okBtn?.focus();
    });
}

// ═══════════════════════════════════════════════════════
// SETTINGS PAGE
// ═══════════════════════════════════════════════════════

async function loadSettings() {
    const result = await apiGet('/api/settings');
    if (result.success) {
        const s = result.result;
        setVal('btc_enabled', s.btc_enabled);
        setVal('eth_enabled', s.eth_enabled);
        setVal('btc_quantity', s.btc_quantity);
        setVal('eth_quantity', s.eth_quantity);
        setVal('breakout_buffer_pct', s.breakout_buffer_pct);
        setVal('cooldown_minutes', s.cooldown_minutes);
        setVal('max_daily_loss', s.max_daily_loss);
        setVal('leverage', s.leverage);
        setVal('candle_resolution', s.candle_resolution || '15m');
        setVal('lookback_candles', s.lookback_candles || 6);

        const bufLabel = document.getElementById('buffer-label');
        if (bufLabel) bufLabel.textContent = `${s.breakout_buffer_pct}%`;

        if (s.has_api_key) {
            const ind = document.getElementById('api-key-status');
            if (ind) {
                ind.textContent = '✓ API Key configured';
                ind.style.color = 'var(--green)';
            }
        }

        const testnetToggle = document.getElementById('testnet_toggle');
        if (testnetToggle) testnetToggle.checked = s.testnet;
    }
}

async function saveSettings() {
    const settings = {
        btc_enabled: getCheckVal('btc_enabled'),
        eth_enabled: getCheckVal('eth_enabled'),
        btc_quantity: parseInt(getVal('btc_quantity')) || 10,
        eth_quantity: parseInt(getVal('eth_quantity')) || 10,
        breakout_buffer_pct: parseFloat(getVal('breakout_buffer_pct')) || 0.1,
        cooldown_minutes: parseInt(getVal('cooldown_minutes')) || 5,
        max_daily_loss: parseFloat(getVal('max_daily_loss')) || 100,
        leverage: parseInt(getVal('leverage')) || 10,
        candle_resolution: getVal('candle_resolution') || '15m',
        lookback_candles: parseInt(getVal('lookback_candles')) || 6,
    };

    const result = await apiPost('/api/settings', settings);
    if (result.success) {
        showToast('Settings saved successfully', 'success');
    } else {
        showToast(`Failed to save: ${result.error}`, 'error');
    }
}

function updateLookbackInfo() {
    const res = getVal('candle_resolution') || '15m';
    const count = parseInt(getVal('lookback_candles')) || 6;
    const mins = { '1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60 };
    const totalMins = count * (mins[res] || 15);
    let timeStr;
    if (totalMins >= 60) {
        const hrs = totalMins / 60;
        timeStr = hrs === Math.floor(hrs) ? `${hrs} hour` : `${hrs.toFixed(1)} hour`;
        if (hrs > 1) timeStr += 's';
    } else {
        timeStr = `${totalMins} minutes`;
    }
    const info = document.getElementById('lookback-info');
    if (info) info.textContent = `${count} \u00d7 ${res} = ${timeStr} range window`;
}

async function saveApiKeys() {
    const apiKey = getVal('api_key');
    const apiSecret = getVal('api_secret');
    const testnet = getCheckVal('testnet_toggle');

    if (!apiKey || !apiSecret) {
        showToast('Please enter both API Key and Secret', 'warning');
        return;
    }

    // Warn if switching to mainnet
    if (!testnet) {
        const confirmed = confirm(
            '⚠️ WARNING: You are switching to MAINNET (real money).\n\n' +
            'This will place REAL orders with REAL funds.\n\n' +
            'Are you absolutely sure?'
        );
        if (!confirmed) return;
    }

    const result = await apiPost('/api/settings/api-keys', {
        api_key: apiKey,
        api_secret: apiSecret,
        testnet: testnet,
    });

    if (result.success) {
        showToast('API keys saved', 'success');
        const ind = document.getElementById('api-key-status');
        if (ind) {
            ind.textContent = '✓ API Key configured';
            ind.style.color = 'var(--green)';
        }
    } else {
        showToast(`Failed: ${result.error}`, 'error');
    }
}

async function testConnection() {
    const btn = document.getElementById('btn-test-conn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Testing...';
    }

    const result = await apiPost('/api/settings/test-connection');

    if (btn) {
        btn.disabled = false;
        btn.textContent = '🔌 Test Connection';
    }

    if (result.success) {
        const balances = result.balances;
        let balText = '';

        if (Array.isArray(balances)) {
            balances.forEach(b => {
                if (b.balance && parseFloat(b.balance) > 0) {
                    balText += `${b.asset_symbol || b.asset_id}: ${parseFloat(b.balance).toFixed(4)}\n`;
                }
            });
        } else if (balances?.result && Array.isArray(balances.result)) {
            balances.result.forEach(b => {
                if (b.balance && parseFloat(b.balance) > 0) {
                    balText += `${b.asset_symbol || b.asset_id}: ${parseFloat(b.balance).toFixed(4)}\n`;
                }
            });
        }

        showToast(`✅ Connection successful!\n${balText || 'Balance retrieved'}`, 'success');
    } else {
        showToast(`❌ ${result.message || 'Connection failed'}`, 'error');
    }
}

// ═══════════════════════════════════════════════════════
// TRADES PAGE
// ═══════════════════════════════════════════════════════

let tradesPage = 1;

async function loadTrades(page = 1) {
    tradesPage = page;
    const symbol = getVal('filter-symbol') || '';
    const startDate = getVal('filter-start') || '';
    const endDate = getVal('filter-end') || '';

    let query = `?page=${page}&page_size=20`;
    if (symbol) query += `&symbol=${symbol}`;
    if (startDate) query += `&start_date=${startDate}`;
    if (endDate) query += `&end_date=${endDate}`;

    const result = await apiGet(`/api/trades${query}`);

    if (result.success) {
        renderTradesTable(result.result);
        renderPagination(result.meta);
    }
}

function renderTradesTable(trades) {
    const tbody = document.getElementById('trades-tbody');
    if (!tbody) return;

    if (!trades || trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 40px; color: var(--text-muted);">No trades found</td></tr>`;
        return;
    }

    tbody.innerHTML = trades.map(t => {
        const dirClass = t.direction.toUpperCase() === 'LONG' ? 'td-long' : 'td-short';
        const pnlClass = t.pnl > 0 ? 'td-pnl-pos' : t.pnl < 0 ? 'td-pnl-neg' : '';
        const pnlText = t.pnl != null ? formatPnL(t.pnl) : '—';
        const feeText = t.fee != null ? formatPrice(t.fee) : '—';
        const time = t.timestamp ? new Date(t.timestamp).toLocaleString() : '—';
        
        let exitBadge = t.exit_type;
        if (t.exit_type === 'TP') {
            exitBadge = `<span style="background: rgba(16, 185, 129, 0.2); color: var(--green); padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600;">Hit TP 🎯</span>`;
        } else if (t.exit_type === 'SL') {
            exitBadge = `<span style="background: rgba(239, 68, 68, 0.2); color: var(--red); padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600;">Hit SL 🛑</span>`;
        } else {
            exitBadge = `<span style="color: var(--text-muted); font-size: 0.8rem;">${t.exit_type}</span>`;
        }

        return `<tr>
            <td>${time}</td>
            <td class="td-symbol">${t.symbol}</td>
            <td class="${dirClass}">${t.direction.toUpperCase() === 'LONG' ? '↑' : '↓'} ${t.direction.toUpperCase()}</td>
            <td>${formatPrice(t.price, t.symbol)}</td>
            <td>${t.size}</td>
            <td class="${pnlClass}">${pnlText}</td>
            <td>${feeText}</td>
            <td>${exitBadge}</td>
        </tr>`;
    }).join('');
}

function renderPagination(meta) {
    const container = document.getElementById('pagination');
    if (!container || !meta) return;

    const { page, total_pages } = meta;
    let html = '';

    html += `<button class="page-btn" onclick="loadTrades(${page - 1})" ${page <= 1 ? 'disabled' : ''}>← Prev</button>`;
    html += `<span class="page-info">Page ${page} of ${total_pages}</span>`;
    html += `<button class="page-btn" onclick="loadTrades(${page + 1})" ${page >= total_pages ? 'disabled' : ''}>Next →</button>`;

    container.innerHTML = html;
}

async function loadTradeStats() {
    const result = await apiGet('/api/trades/stats');
    if (result.success) {
        const s = result.result;
        const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };

        el('stat-total-trades', s.total_trades || 0);
        el('stat-win-rate', `${s.win_rate || 0}%`);

        const totalPnlEl = document.getElementById('stat-total-pnl');
        if (totalPnlEl) {
            totalPnlEl.textContent = formatPnL(s.total_pnl || 0);
            totalPnlEl.style.color = (s.total_pnl || 0) > 0 ? 'var(--green)'
                : (s.total_pnl || 0) < 0 ? 'var(--red)' : '';
        }

        const todayPnlEl = document.getElementById('stat-today-pnl');
        if (todayPnlEl) {
            todayPnlEl.textContent = formatPnL(s.today_pnl || 0);
            todayPnlEl.style.color = (s.today_pnl || 0) > 0 ? 'var(--green)'
                : (s.today_pnl || 0) < 0 ? 'var(--red)' : '';
        }
    }
}

function exportCSV() {
    const symbol = getVal('filter-symbol') || '';
    const start = getVal('filter-start') || '';
    const end = getVal('filter-end') || '';

    let query = '?';
    if (symbol) query += `symbol=${symbol}&`;
    if (start) query += `start_date=${start}&`;
    if (end) query += `end_date=${end}&`;

    window.open(`/api/trades/export${query}`, '_blank');
}

// ═══════════════════════════════════════════════════════
// POLLING FALLBACK
// ═══════════════════════════════════════════════════════

async function pollStatus() {
    const result = await apiGet('/api/status');
    if (result.success) {
        handleStatusUpdate(result.result);
    }
}

function startPolling() {
    if (statusPollTimer) return;
    statusPollTimer = setInterval(pollStatus, 5000);
}

// ═══════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════

function showToast(message, type = 'info', duration = 5000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
    toast.innerHTML = `
        <span>${icons[type] || ''}</span>
        <span style="flex: 1; white-space: pre-line;">${message}</span>
        <button class="toast-close" onclick="this.parentElement.remove()">×</button>
    `;

    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('toast-out');
        setTimeout(() => toast.remove(), 200);
    }, duration);
}

// ═══════════════════════════════════════════════════════
// UTILITY FUNCTIONS
// ═══════════════════════════════════════════════════════

function formatPrice(price, symbol = '') {
    if (price == null || price === '') return '—';
    const p = parseFloat(price);
    if (!Number.isFinite(p)) return '—';
    return `$${p.toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    })}`;
}

// Returns "+$1,234.56" / "-$1,234.56" / "$0.00" — caller can apply
// .td-pnl-pos / .td-pnl-neg classes for color.
function formatPnL(value) {
    if (value == null || value === '' || !Number.isFinite(parseFloat(value))) return '—';
    const v = parseFloat(value);
    const sign = v > 0 ? '+' : v < 0 ? '-' : '';
    return `${sign}$${Math.abs(v).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    })}`;
}

// Relative-time string: "live", "3s ago", "1m ago"...
function formatAgo(ms) {
    if (ms == null) return '';
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 1) return 'live';
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    return `${Math.floor(m / 60)}h ago`;
}

function formatVolume(vol) {
    if (!vol) return '—';
    const v = parseFloat(vol);
    if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
    if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
    if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
    return v.toString();
}

function getVal(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
}

function setVal(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') {
        el.checked = !!val;
    } else if (el.type === 'range') {
        el.value = val;
    } else {
        el.value = val;
    }
}

function getCheckVal(id) {
    const el = document.getElementById(id);
    return el ? el.checked : false;
}

// ═══════════════════════════════════════════════════════
// KPI STRIP
// ═══════════════════════════════════════════════════════

function updateKpiStrip(data) {
    const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    el('kpi-signals', data.daily_signals ?? data.total_signals ?? 0);
    el('kpi-trades',  data.daily_trades  ?? data.total_trades  ?? 0);

    const pnl = data.position_manager?.daily_pnl || 0;
    const pnlEl = document.getElementById('kpi-daily-pnl');
    if (pnlEl) {
        pnlEl.textContent = formatPnL(pnl);
        pnlEl.style.color = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : '';
    }
}

// ═══════════════════════════════════════════════════════
// ACCOUNT & PERFORMANCE PANEL
// ═══════════════════════════════════════════════════════

const ASSET_ICONS = {
    'BTC': '₿', 'ETH': '⟠', 'USDT': '₮', 'USD': '$',
    'INR': '₹', 'USDC': '💵',
};

function formatPnLValue(val) {
    const n = parseFloat(val || 0);
    const sign = n >= 0 ? '+' : '';
    return `${sign}$${n.toFixed(2)}`;
}

function pnlColor(val) {
    const n = parseFloat(val || 0);
    if (n > 0) return 'var(--green)';
    if (n < 0) return 'var(--red)';
    return 'var(--text-muted)';
}

async function loadAccountSummary() {
    const container = document.getElementById('account-summary-container');
    if (!container) return;

    const refreshBtn = document.getElementById('btn-refresh-account');
    if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.textContent = '↻ Loading...'; }

    const result = await apiGet('/api/account-summary', { quiet: true });

    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = '↻ Refresh'; }

    if (!result.success || !result.result) {
        container.innerHTML = `
            <div class="wallet-empty">
                <div class="wallet-empty-icon">🔑</div>
                <div>Configure API keys to view account</div>
            </div>`;
        return;
    }

    const data = result.result;
    const wallet = data.wallet || { balances: [], total_usd: 0 };
    const stats = data.stats || {};
    const risk = data.risk || {};

    // Today's PnL (hero number)
    const todayPnl = stats.today_pnl || 0;
    const totalPnl = stats.total_pnl || 0;

    // Daily loss limit progress
    const maxLoss = risk.max_daily_loss || 100;
    const dailyPnl = risk.daily_pnl || 0;
    const lossPct = Math.min(risk.daily_loss_pct || 0, 100);
    const lossBarColor = lossPct > 75 ? 'var(--red)' : lossPct > 40 ? '#f59e0b' : 'var(--green)';

    // Wallet balances mini row
    const walletRows = wallet.balances.map(b => {
        const icon = ASSET_ICONS[b.symbol] || b.symbol.charAt(0);
        const usdVal = b.usd_value ? `$${b.usd_value.toFixed(2)}` : '–';
        return `
        <div class="acct-wallet-item">
            <div class="acct-wallet-icon">${icon}</div>
            <div class="acct-wallet-details">
                <div class="acct-wallet-top">
                    <span class="acct-wallet-sym">${b.symbol}</span>
                    <span class="acct-wallet-val">${b.balance.toFixed(4)}</span>
                </div>
                <div class="acct-wallet-bottom">
                    <span class="acct-wallet-usd">${usdVal}</span>
                    <span class="acct-wallet-avail">Avail: ${b.available.toFixed(4)}</span>
                </div>
            </div>
        </div>`;
    }).join('');

    // Helper to format INR value with Lakhs/Crores just like Delta Exchange
    function formatINR(usd) {
        const inr = usd * 85; // Delta default conversion rate
        if (inr >= 10000000) return `₹${(inr / 10000000).toFixed(2)}Cr`;
        if (inr >= 100000) return `₹${(inr / 100000).toFixed(2)}L`;
        return `₹${inr.toLocaleString('en-IN', {maximumFractionDigits: 2})}`;
    }

    container.innerHTML = `
        <!-- Hero: Account Value -->
        <div class="acct-hero">
            <div class="acct-hero-label">Account Value</div>
            <div class="acct-hero-value" style="color: var(--text-bright)">
                ${formatINR(wallet.total_usd)}
            </div>
            <div class="acct-hero-usd" style="font-family: 'JetBrains Mono', monospace; font-size: 1rem; color: var(--text-muted); margin-bottom: 12px; font-weight: 500;">
                $${wallet.total_usd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}
            </div>
            <div class="acct-hero-sub">
                <span class="acct-badge">Today's P&L: <span style="color: ${pnlColor(todayPnl)}">${formatPnLValue(todayPnl)}</span></span>
                <span class="acct-badge">Win Rate: ${stats.today_win_rate || 0}%</span>
            </div>
        </div>

        <!-- Stats Grid -->
        <div class="acct-stats-grid">
            <div class="acct-stat">
                <div class="acct-stat-label">All-Time P&L</div>
                <div class="acct-stat-value" style="color: ${pnlColor(totalPnl)}">${formatPnLValue(totalPnl)}</div>
            </div>
            <div class="acct-stat">
                <div class="acct-stat-label">Win Rate</div>
                <div class="acct-stat-value">${stats.win_rate || 0}%</div>
            </div>
            <div class="acct-stat">
                <div class="acct-stat-label">Trades (W/L)</div>
                <div class="acct-stat-value">${stats.total_closed || 0} <span class="acct-wl">(${stats.total_wins || 0}/${stats.total_losses || 0})</span></div>
            </div>
            <div class="acct-stat">
                <div class="acct-stat-label">Profit Factor</div>
                <div class="acct-stat-value">${stats.profit_factor || '–'}</div>
            </div>
        </div>

        <!-- Daily Loss Limit Bar -->
        <div class="acct-risk-bar">
            <div class="acct-risk-header">
                <span class="risk-title">Daily Loss Limit</span>
                <span class="risk-value" style="color: ${pnlColor(dailyPnl)}">${formatPnLValue(dailyPnl)} / -$${maxLoss.toFixed(0)}</span>
            </div>
            <div class="acct-progress-track">
                <div class="acct-progress-fill" style="width: ${lossPct}%; background: ${lossBarColor}"></div>
            </div>
        </div>

        <!-- Wallet Balances -->
        <div class="acct-wallet-section">
            <div class="acct-wallet-header">💼 Wallet Balances</div>
            <div class="acct-wallet-list">
                ${walletRows || '<div class="acct-wallet-empty">No balances</div>'}
            </div>
        </div>
    `;
}

// Keep old name as alias for backward compatibility
async function loadBalance() { return loadAccountSummary(); }


// ═══════════════════════════════════════════════════════
// DASHBOARD STATS (from /api/trades/stats)
// ═══════════════════════════════════════════════════════

async function loadDashboardStats() {
    const result = await apiGet('/api/trades/stats', { quiet: true });
    if (!result.success) return;
    const s = result.result;

    const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    el('kpi-winrate', `${s.win_rate || 0}%`);

    const totalPnlEl = document.getElementById('kpi-total-pnl');
    if (totalPnlEl) {
        totalPnlEl.textContent = formatPnL(s.total_pnl || 0);
        totalPnlEl.style.color = (s.total_pnl || 0) > 0 ? 'var(--green)'
            : (s.total_pnl || 0) < 0 ? 'var(--red)' : '';
    }

    const todayPnlEl = document.getElementById('kpi-daily-pnl');
    if (todayPnlEl && s.today_pnl != null) {
        todayPnlEl.textContent = formatPnL(s.today_pnl || 0);
        todayPnlEl.style.color = (s.today_pnl || 0) > 0 ? 'var(--green)'
            : (s.today_pnl || 0) < 0 ? 'var(--red)' : '';
    }
}

// ═══════════════════════════════════════════════════════
// ACTIVITY LOG
// ═══════════════════════════════════════════════════════

function appendActivityLog(message, type = 'system', icon = 'ℹ️') {
    const log = document.getElementById('activity-log');
    if (!log) return;

    // Remove empty-state placeholder if present
    const empty = log.querySelector('.activity-log-empty');
    if (empty) empty.remove();

    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-US', { hour12: false });

    const entry = document.createElement('div');
    entry.className = `activity-entry ${type}`;
    entry.innerHTML = `
        <div class="activity-entry-icon">${icon}</div>
        <div class="activity-entry-body">
            <div class="activity-entry-msg">${message}</div>
            <div class="activity-entry-time">${timeStr}</div>
        </div>`;

    log.insertBefore(entry, log.firstChild);

    // Cap at MAX_LOG_ENTRIES
    while (log.children.length > MAX_LOG_ENTRIES) {
        log.removeChild(log.lastChild);
    }
}

function clearActivityLog() {
    const log = document.getElementById('activity-log');
    if (!log) return;
    log.innerHTML = `<div class="activity-log-empty">Log cleared</div>`;
}

// ═══════════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════════


document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;

    if (path === '/' || path === '/index.html' || path === '') {
        // Dashboard
        connectWebSocket();
        startPolling();
        pollStatus();   // Immediate first fetch

        // Load balance and trade stats on startup
        loadBalance();
        loadDashboardStats();

        // Seed activity log
        appendActivityLog('Dashboard connected', 'system', '🟢');

        // Refresh balance every 30 s
        setInterval(loadBalance, 30_000);
        // Refresh trade stats every 60 s
        setInterval(loadDashboardStats, 60_000);

        // 1 Hz refresh of the "live / Ns ago" pill
        setInterval(updateLastUpdatePill, 1000);

    } else if (path === '/settings' || path === '/settings.html') {
        loadSettings();
    } else if (path === '/trades' || path === '/trades.html') {
        loadTrades();
        loadTradeStats();
    }

    // Buffer slider live label update
    const bufferSlider = document.getElementById('breakout_buffer_pct');
    if (bufferSlider) {
        bufferSlider.addEventListener('input', (e) => {
            const label = document.getElementById('buffer-label');
            if (label) label.textContent = `${e.target.value}%`;
        });
    }

    // Candle settings live label update
    const candleRes = document.getElementById('candle_resolution');
    const lookbackSel = document.getElementById('lookback_candles');
    if (candleRes) candleRes.addEventListener('change', updateLookbackInfo);
    if (lookbackSel) lookbackSel.addEventListener('change', updateLookbackInfo);
});
