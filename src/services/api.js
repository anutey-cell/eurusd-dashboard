// ─── Base config ──────────────────────────────────────────────────────────────
const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const PREFIX = '/api/v1';
const DEFAULT_TIMEOUT_MS = 8000;

async function apiFetch(path, options = {}) {
  const url = `${API_BASE}${PREFIX}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);

  try {
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      ...options,
    });
    clearTimeout(timer);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') throw new Error('Request timed out');
    throw err;
  }
}

// ─── Public API functions ─────────────────────────────────────────────────────

export async function getHealth() {
  return apiFetch('/health');
}

/** Returns { dataMode, fxProvider, calendarProvider, database, brokerExecutionEnabled, timestamp } */
export async function getDataStatus() {
  const res = await apiFetch('/health');
  return {
    dataMode:               res.data_mode,
    fxProvider:             res.fx_provider,
    calendarProvider:       res.calendar_provider,
    database:               res.database,
    brokerExecutionEnabled: res.broker_execution_enabled,
    timestamp:              res.timestamp,
  };
}

export async function getPairs() {
  return apiFetch('/pairs');
}

export async function getCandles({ pair = 'eurusd', interval = 'H4', limit = 200 } = {}) {
  const params = new URLSearchParams({ pair, interval, limit });
  return apiFetch(`/candles?${params}`);
}

export async function getMacroCalendar({ date } = {}) {
  const q = date ? `?date=${encodeURIComponent(date)}` : '';
  const res = await apiFetch(`/calendar${q}`);
  return adaptCalendar(res);
}

export async function getSignal() {
  const res = await apiFetch('/signal/current');
  return adaptSignal(res);
}

export async function getSignalHistory({ page = 1, pageSize = 20, direction, result } = {}) {
  const params = new URLSearchParams({ page, page_size: pageSize });
  if (direction) params.set('direction', direction);
  if (result) params.set('result', result);
  const res = await apiFetch(`/signal/history?${params}`);
  return adaptHistory(res);
}

export async function getAnalytics() {
  const res = await apiFetch('/analytics');
  return adaptAnalytics(res);
}

export async function runBacktest({ pair = 'EUR/USD', timeframe = 'H4', lookback = 500 } = {}) {
  const params = new URLSearchParams({ pair, timeframe, lookback });
  return apiFetch(`/backtest/run?${params}`);
}

export async function runOptimize({ pair = 'EUR/USD', timeframe = 'H4', lookback = 1000 } = {}) {
  const params = new URLSearchParams({ pair, timeframe, lookback });
  return apiFetch(`/backtest/optimize?${params}`);
}

export async function getExecutionStatus() {
  return apiFetch('/execution/status');
}

export async function triggerKillSwitch() {
  return apiFetch('/execution/kill-switch', { method: 'POST' });
}

export async function confirmSignal(signalId) {
  return apiFetch(`/signal/${signalId}/confirm`, { method: 'POST' });
}

export async function updateSignalResult(signalId, resultData) {
  return apiFetch(`/signal/${signalId}/result`, {
    method: 'PUT',
    body: JSON.stringify(resultData),
  });
}

/** Fetch paper-trading journal from the real database (/signal/db/history) */
export async function getDbSignalHistory({ page = 1, pageSize = 20, signal, result, pair } = {}) {
  const params = new URLSearchParams({ page, page_size: pageSize });
  if (signal) params.set('signal', signal);
  if (result)  params.set('result', result);
  if (pair)   params.set('pair', pair);
  const res = await apiFetch(`/signal/db/history?${params}`);
  // Schemas use alias_generator=to_camel so backend emits camelCase already
  return {
    total:    res.data.total,
    page:     res.data.page,
    pageSize: res.data.pageSize,  // alias_generator=to_camel → pageSize not page_size
    signals:  res.data.signals,   // array of SignalRead (camelCase)
  };
}

export async function analyzeSignalForPair(pair = 'eurusd', macroEvents = []) {
  const res = await apiFetch('/signal/analyze', {
    method: 'POST',
    body: JSON.stringify(macroEvents),
  });
  // res.data is SignalAnalysisOutput — add pair to it if missing
  if (res.data && !res.data.pair) res.data.pair = pair;
  return res.data;
}

/** Log the final outcome of a paper trade (PUT /signal/{id}/result) */
export async function logSignalResult(signalId, { result, exitPrice, pips, pnl = null, notes = null }) {
  return apiFetch(`/signal/${signalId}/result`, {
    method: 'PUT',
    // Backend schema accepts both snake_case and camelCase (populate_by_name=True)
    body: JSON.stringify({ result, exitPrice, pips, pnl, notes }),
  });
}

// ─── Adapters: snake_case (backend) → camelCase (frontend) ───────────────────

function adaptSignal(res) {
  const s = res.data.signal;
  const p = res.data.trade_plan;
  return {
    currentSignal: {
      direction:  s.direction,
      strength:   s.strength,
      confidence: s.confidence,
      timestamp:  s.timestamp,
      session:    s.session,
      timeframe:  s.timeframe,
      price:      s.price,
      change:     s.change,
      changePct:  s.change_pct,
      factors:    s.factors,   // {name, value, score, positive} — already camelCase-friendly
    },
    tradePlan: {
      entry:        p.entry,
      stopLoss:     p.stop_loss,
      stopLossPips: p.stop_loss_pips,
      targets:      p.targets,  // [{label, price, rr, pips, partial}]
      riskPercent:  p.risk_percent,
      positionSize: p.position_size,
      accountSize:  p.account_size,
      riskAmount:   p.risk_amount,
      validity:     p.validity,
      notes:        p.notes,
    },
  };
}

function adaptHistory(res) {
  // Handle both /signal/history (trades array) and /signal/db/history (signals array)
  const data = res.data ?? res;
  const rawTrades = data.trades ?? data.signals ?? [];
  return {
    total:    data.total    ?? rawTrades.length,
    page:     data.page     ?? 1,
    pageSize: data.page_size ?? data.pageSize ?? 20,
    trades: rawTrades.map(t => ({
      id:         t.id,
      date:       t.date,
      time:       t.time,
      dir:        t.direction ?? t.signal,
      entry:      t.entry,
      exit:       t.exit ?? t.exitPrice,
      pips:       t.pips,
      rr:         t.rr,
      result:     t.result,
      pnl:        t.pnl,
      confidence: t.confidence ?? t.qualityScore,
    })),
  };
}

function adaptAnalytics(res) {
  // Backend emits camelCase (alias_generator=to_camel) — pass through directly.
  // Unwrap the APIResponse envelope and return the data object as-is.
  return res.data;
}

function adaptCalendar(res) {
  return {
    newsItems: res.data.events.map(e => ({
      id:       e.id,
      time:     e.time,
      currency: e.currency,
      event:    e.event,
      impact:   e.impact,
      forecast: e.forecast ?? '—',
      previous: e.previous ?? '—',
      actual:   e.actual,
      pending:  e.pending,
      beat:     e.beat,
    })),
    recentNews: res.data.news,
  };
}
