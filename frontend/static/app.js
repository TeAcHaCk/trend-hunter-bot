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
};

let ws = null;
let reconnectTimer = null;
let statusPollTimer = null;

// Chart state
let klineChart = null;
let currentChartSymbol = 'BTCUSD';
let currentTimeframe = '5m';
let chartRefreshTimer = null;

// ═══════════════════════════════════════════════════════
// API HELPERS
// ═══════════════════════════════════════════════════════

async function api(endpoint, options = {}) {
    try {
        const response = await fetch(`${API_BASE}${endpoint}`, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        const data = await response.json();
        return data;
    } catch (error) {
        console.error(`API error [${endpoint}]:`, error);
        return { success: false, error: error.message };
    }
}

async function apiGet(endpoint) {
    return api(endpoint);
}

async function apiPost(endpoint, body = {}) {
    return api(endpoint, {
        method: 'POST',
        body: JSON.stringify(body),
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

// ═══════════════════════════════════════════════════════
// DATA HANDLERS
// ═══════════════════════════════════════════════════════

function handlePriceUpdate(data) {
    if (!data || !data.symbol) return;
    const prev = state.prices[data.symbol]?.price || data.price;
    state.prices[data.symbol] = data;
    updatePriceDisplay(data.symbol, data, prev);
}

function handleStatusUpdate(data) {
    if (!data) return;
    state.botStatus = data.state || 'STOPPED';
    state.strategies = data.strategies || {};
    state.positions = data.position_manager?.positions || {};
    state.testnet = data.testnet ?? true;
    state.lastUpdate = new Date().toISOString();

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
}

// ═══════════════════════════════════════════════════════
// UI UPDATES — Dashboard
// ═══════════════════════════════════════════════════════

function updatePriceDisplay(symbol, data, prevPrice) {
    const priceEl = document.getElementById(`price-${symbol}`);
    if (!priceEl) return;

    const price = data.close || data.price || data.mark_price || 0;
    const direction = price > prevPrice ? 'up' : price < prevPrice ? 'down' : '';

    // Animate price change
    priceEl.textContent = formatPrice(price, symbol);
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

                // Unrealized PnL
                const pnlEl = document.getElementById(`trade-pnl-${symbol}`);
                if (pnlEl && strat.entry_price && price) {
                    let pnl = 0;
                    if (strat.trade_direction === 'LONG') pnl = price - strat.entry_price;
                    else pnl = strat.entry_price - price;
                    pnl *= (strat.quantity || 1);
                    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
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
                if (buyPrice && strat.breakout_high) buyPrice.textContent = formatPrice(strat.breakout_high, symbol);
                if (buyId && strat.long_order_id) buyId.textContent = `#${strat.long_order_id}`;

                // Sell order
                const sellPrice = document.getElementById(`order-sell-price-${symbol}`);
                const sellId = document.getElementById(`order-sell-id-${symbol}`);
                if (sellPrice && strat.breakout_low) sellPrice.textContent = formatPrice(strat.breakout_low, symbol);
                if (sellId && strat.short_order_id) sellId.textContent = `#${strat.short_order_id}`;

                // Countdown timer (30 min = 1800s from placement)
                const timerEl = document.getElementById(`order-timer-${symbol}`);
                if (timerEl) {
                    // We track locally when we first saw orders_placed
                    if (!window._orderStartTime) window._orderStartTime = {};
                    if (!window._orderStartTime[symbol]) {
                        window._orderStartTime[symbol] = Date.now();
                    }
                    const elapsedMs = Date.now() - window._orderStartTime[symbol];
                    const remainingSec = Math.max(0, 1800 - Math.floor(elapsedMs / 1000));
                    const mins = Math.floor(remainingSec / 60);
                    const secs = remainingSec % 60;
                    timerEl.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;

                    // Change color when running low
                    if (remainingSec < 120) {
                        timerEl.style.color = 'var(--red)';
                    } else if (remainingSec < 600) {
                        timerEl.style.color = 'var(--amber)';
                    }
                }
            }
        } else {
            // No trade, no orders
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

    el('stat-signals', data.total_signals || 0);
    el('stat-trades', data.total_trades || 0);

    const pnl = data.position_manager?.daily_pnl || 0;
    const pnlEl = document.getElementById('stat-daily-pnl');
    if (pnlEl) {
        pnlEl.textContent = `$${pnl.toFixed(2)}`;
        pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    }

    if (data.last_check_time) {
        el('stat-last-check', new Date(data.last_check_time + 'Z').toLocaleTimeString());
    }
}

// ═══════════════════════════════════════════════════════
// BOT CONTROLS
// ═══════════════════════════════════════════════════════

async function startBot() {
    const btn = document.getElementById('btn-start');
    if (btn) btn.disabled = true;

    const result = await apiPost('/api/bot/start');
    if (result.success) {
        showToast('Bot started successfully', 'success');
    } else {
        showToast(`Failed to start bot: ${result.error || 'Unknown error'}`, 'error');
        if (btn) btn.disabled = false;
    }
}

async function stopBot() {
    const result = await apiPost('/api/bot/stop');
    if (result.success) {
        showToast('Bot stopped', 'warning');
    } else {
        showToast(`Failed to stop: ${result.error}`, 'error');
    }
}

async function pauseBot() {
    const result = await apiPost('/api/bot/pause');
    if (result.success) {
        showToast(result.message, 'info');
    } else {
        showToast(`Failed: ${result.error}`, 'error');
    }
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
        const dirClass = t.direction === 'LONG' ? 'td-long' : 'td-short';
        const pnlClass = t.pnl > 0 ? 'td-pnl-pos' : t.pnl < 0 ? 'td-pnl-neg' : '';
        const pnlText = t.pnl != null ? `$${t.pnl.toFixed(2)}` : '—';
        const time = t.timestamp ? new Date(t.timestamp).toLocaleString() : '—';

        return `<tr>
            <td>${time}</td>
            <td class="td-symbol">${t.symbol}</td>
            <td class="${dirClass}">${t.direction === 'LONG' ? '↑' : '↓'} ${t.direction}</td>
            <td>${formatPrice(t.entry_price, t.symbol)}</td>
            <td>${t.exit_price ? formatPrice(t.exit_price, t.symbol) : '—'}</td>
            <td>${t.quantity}</td>
            <td class="${pnlClass}">${pnlText}</td>
            <td>${t.status}</td>
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
            totalPnlEl.textContent = `$${(s.total_pnl || 0).toFixed(2)}`;
            totalPnlEl.style.color = s.total_pnl >= 0 ? 'var(--green)' : 'var(--red)';
        }

        const todayPnlEl = document.getElementById('stat-today-pnl');
        if (todayPnlEl) {
            todayPnlEl.textContent = `$${(s.today_pnl || 0).toFixed(2)}`;
            todayPnlEl.style.color = s.today_pnl >= 0 ? 'var(--green)' : 'var(--red)';
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
    if (!price) return '—';
    const p = parseFloat(price);
    if (symbol.startsWith('ETH')) return `$${p.toFixed(2)}`;
    return `$${p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
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
// INITIALIZATION
// ═══════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════
// KLINECHART
// ═══════════════════════════════════════════════════════

function initChart() {
    const container = document.getElementById('kline-chart');
    if (!container || typeof klinecharts === 'undefined') {
        console.warn('KLineChart not available');
        return;
    }

    klineChart = klinecharts.init('kline-chart', {
        styles: {
            grid: {
                show: true,
                horizontal: { color: 'rgba(56, 68, 100, 0.15)' },
                vertical: { color: 'rgba(56, 68, 100, 0.15)' },
            },
            candle: {
                type: 'candle_solid',
                priceMark: { show: true, high: { show: true }, low: { show: true }, last: { show: true } },
                bar: {
                    upColor: '#10b981',
                    downColor: '#ef4444',
                    upBorderColor: '#10b981',
                    downBorderColor: '#ef4444',
                    upWickColor: '#10b981',
                    downWickColor: '#ef4444',
                },
            },
            indicator: {
                ohlc: { upColor: '#10b981', downColor: '#ef4444' },
            },
            xAxis: {
                axisLine: { color: 'rgba(56, 68, 100, 0.3)' },
                tickText: { color: '#8b949e' },
            },
            yAxis: {
                axisLine: { color: 'rgba(56, 68, 100, 0.3)' },
                tickText: { color: '#8b949e' },
            },
            separator: { color: 'rgba(56, 68, 100, 0.2)' },
            crosshair: {
                show: true,
                horizontal: { line: { color: 'rgba(0, 212, 255, 0.3)' } },
                vertical: { line: { color: 'rgba(0, 212, 255, 0.3)' } },
            },
        },
    });

    // Set data loader for KLineChart v10
    klineChart.setDataLoader({
        getBars: async ({ callback, symbol, period }) => {
            try {
                const tf = currentTimeframe || '15m';
                const sym = currentChartSymbol || 'BTCUSD';
                const result = await apiGet(`/api/candles/${sym}?resolution=${tf}&count=100`);
                if (result.success && result.result && result.result.length > 0) {
                    callback(result.result);
                } else {
                    callback([]);
                }
            } catch (e) {
                console.error('Chart data fetch error:', e);
                callback([]);
            }
        }
    });

    // Set symbol and period to trigger data load
    klineChart.setSymbol({ ticker: currentChartSymbol });
    klineChart.setPeriod({ span: parseInt(currentTimeframe) || 5, type: 'minute' });

    // Auto-refresh chart
    chartRefreshTimer = setInterval(refreshChart, 30000);
}

function refreshChart() {
    if (!klineChart) return;
    // Re-trigger data load by re-setting the symbol
    klineChart.setSymbol({ ticker: currentChartSymbol });
    klineChart.setPeriod(getPeriod(currentTimeframe));
}

function getPeriod(tf) {
    const map = {
        '1m': { span: 1, type: 'minute' },
        '5m': { span: 5, type: 'minute' },
        '15m': { span: 15, type: 'minute' },
        '30m': { span: 30, type: 'minute' },
        '1h': { span: 1, type: 'hour' },
        '1d': { span: 1, type: 'day' },
    };
    return map[tf] || { span: 15, type: 'minute' };
}

function updateChartOverlays(strat) {
    if (!klineChart) return;

    // Remove old overlays
    klineChart.removeOverlay();

    // Add breakout high line
    if (strat.breakout_high) {
        klineChart.createOverlay({
            name: 'horizontalStraightLine',
            points: [{ value: strat.breakout_high }],
            styles: {
                line: { color: '#10b981', size: 1, style: 'dashed' },
            },
            lock: true,
        });
    }

    // Add breakout low line
    if (strat.breakout_low) {
        klineChart.createOverlay({
            name: 'horizontalStraightLine',
            points: [{ value: strat.breakout_low }],
            styles: {
                line: { color: '#ef4444', size: 1, style: 'dashed' },
            },
            lock: true,
        });
    }

    // Add SL/TP if in trade
    if (strat.in_trade) {
        if (strat.stop_loss) {
            klineChart.createOverlay({
                name: 'horizontalStraightLine',
                points: [{ value: strat.stop_loss }],
                styles: { line: { color: '#ef4444', size: 2, style: 'solid' } },
                lock: true,
            });
        }
        if (strat.take_profit) {
            klineChart.createOverlay({
                name: 'horizontalStraightLine',
                points: [{ value: strat.take_profit }],
                styles: { line: { color: '#10b981', size: 2, style: 'solid' } },
                lock: true,
            });
        }
        if (strat.entry_price) {
            klineChart.createOverlay({
                name: 'horizontalStraightLine',
                points: [{ value: strat.entry_price }],
                styles: { line: { color: '#00d4ff', size: 1, style: 'solid' } },
                lock: true,
            });
        }
    }
}

function switchChart(symbol) {
    currentChartSymbol = symbol;

    // Update tab active state
    document.querySelectorAll('.chart-tab').forEach(t => t.classList.remove('active'));
    const tab = document.getElementById(`tab-${symbol}`);
    if (tab) tab.classList.add('active');

    refreshChart();
}

function changeTimeframe(tf) {
    currentTimeframe = tf;

    // Update button active state
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    const btn = document.getElementById(`tf-${tf}`);
    if (btn) btn.classList.add('active');

    refreshChart();
}


document.addEventListener('DOMContentLoaded', () => {
    // Determine which page we're on
    const path = window.location.pathname;

    if (path === '/' || path === '/index.html' || path === '') {
        // Dashboard
        connectWebSocket();
        startPolling();
        pollStatus(); // Immediate first fetch
        initChart();  // Initialize KLineChart
    } else if (path === '/settings' || path === '/settings.html') {
        // Settings
        loadSettings();
    } else if (path === '/trades' || path === '/trades.html') {
        // Trades
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
