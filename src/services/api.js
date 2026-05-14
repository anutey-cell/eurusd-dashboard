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

export async function getCandles({ pair = 'xauusd', interval = 'H4', limit = 200 } = {}) {
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

/**
 * Fetch live ICT engine output for a specific pair.
 * Calls GET /signal/{pairCode} — pair-aware, returns live engine data
 * shaped to match what SignalCard + TradePlanCard expect.
 */
export async function getSignalForPair(pairCode = 'xauusd') {
  const res = await apiFetch(`/signal/${pairCode}`);
  return adaptLiveSignal(res);
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

export async function runBacktest({ pair = 'XAU/USD', timeframe = 'H4', lookback = 500 } = {}) {
  const params = new URLSearchParams({ pair, timeframe, lookback });
  return apiFetch(`/backtest/run?${params}`);
}

export async function runOptimize({ pair = 'XAU/USD', timeframe = 'H4', lookback = 1000 } = {}) {
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

export async function analyzeSignalForPair(pair = 'xauusd', macroEvents = []) {
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

// ─── Institutional Scanner API ────────────────────────────────────────────────

/**
 * GET /scan/xauusd — return cached or fresh institutional scan.
 * The backend caches for SCAN_CACHE_TTL_SECONDS (default 45 s).
 */
export async function getScan() {
  const res = await apiFetch('/scan/xauusd');
  return res.data ?? res;
}

/**
 * POST /scan/xauusd/force — force a fresh scan.
 * Called by the "Scan Institutional Setup" button.
 */
export async function forceScan() {
  const res = await apiFetch('/scan/xauusd/force', { method: 'POST' });
  return res.data ?? res;
}

/** GET /scan/history — last N scan records from DB. */
export async function getScanHistory(limit = 20) {
  const res = await apiFetch(`/scan/history?limit=${limit}`);
  return res.data ?? res;
}

/** GET /scan/status — scanner health + cache info. */
export async function getScanStatus() {
  const res = await apiFetch('/scan/status');
  return res.data ?? res;
}

/** GET /market-view/xauusd — latest stored market view from DB. */
export async function getMarketView() {
  const res = await apiFetch('/market-view/xauusd');
  return res.data ?? res;
}

/** GET /scan/backtest — historical replay of the scanner. */
export async function getScanBacktest(lookback = 100) {
  const res = await apiFetch(`/scan/backtest?lookback=${lookback}`);
  return res.data ?? res;
}

// ─── XAU/USD Backtest API ─────────────────────────────────────────────────────

/**
 * Run a strict XAU/USD backtest. All params are query string filters.
 * Returns the full diagnostic dict: summary, equityCurve, trades, skipped, breakdowns, reliability.
 */
export async function runXauusdBacktest(params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') {
      q.set(k, String(v));
    }
  }
  const res = await apiFetch(`/backtest/xauusd?${q}`);
  return res.data ?? res;
}

/** Upload a historical XAU/USD candle CSV. file = File, timeframe = "M15" etc. */
export async function importBacktestCsv(file, timeframe = 'M15') {
  const fd = new FormData();
  fd.append('file', file);
  const url = `${API_BASE}${PREFIX}/backtest/import-csv?timeframe=${encodeURIComponent(timeframe)}`;
  const res = await fetch(url, { method: 'POST', body: fd });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = body.message || body.detail || `HTTP ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return body.data ?? body;
}

/** GET /backtest/data-status — historical candle availability for chosen timeframe. */
export async function getBacktestDataStatus(timeframe = 'M15') {
  const res = await apiFetch(`/backtest/data-status?timeframe=${encodeURIComponent(timeframe)}`);
  return res.data ?? res;
}

/** POST /backtest/seed-historical — seed deterministic 365-day dataset + macro calendar. */
export async function seedHistoricalData({ days = 365, timeframe = 'M15', seed = 42, force = false } = {}) {
  const q = new URLSearchParams({ days, timeframe, seed, force });
  const res = await apiFetch(`/backtest/seed-historical?${q}`, { method: 'POST' });
  return res.data ?? res;
}

/** GET /backtest/runs — list saved backtest runs (newest first). */
export async function listBacktestRuns(limit = 20) {
  const res = await apiFetch(`/backtest/runs?limit=${limit}`);
  return res.data ?? res;
}

/** GET /backtest/runs/:id — full saved backtest payload. */
export async function getBacktestRun(id) {
  const res = await apiFetch(`/backtest/runs/${id}`);
  return res.data ?? res;
}

/** DELETE /backtest/runs/:id — remove a saved backtest run. */
export async function deleteBacktestRun(id) {
  const res = await apiFetch(`/backtest/runs/${id}`, { method: 'DELETE' });
  return res.data ?? res;
}

// ─── Paper Observation API ─────────────────────────────────────────────────────

/** GET /observations — list paper observations (auto-logged SIGNAL_READY states) */
export async function getPaperObservations({ limit = 50, resolved = 'all' } = {}) {
  const res = await apiFetch(`/observations?limit=${limit}&resolved=${resolved}`);
  return res.data ?? res;
}

/** GET /observations/stats — running WR/expectancy + progress to n=30 */
export async function getPaperObservationStats() {
  const res = await apiFetch('/observations/stats');
  return res.data ?? res;
}

/** POST /observations/resolve — forward-walk pending obs to determine outcomes */
export async function resolvePaperObservations() {
  const res = await apiFetch('/observations/resolve', { method: 'POST' });
  return res.data ?? res;
}

/** DELETE /observations/:id */
export async function deletePaperObservation(id) {
  const res = await apiFetch(`/observations/${id}`, { method: 'DELETE' });
  return res.data ?? res;
}

// ─── MT5 API ──────────────────────────────────────────────────────────────────

export async function getMT5Status() {
  return apiFetch('/mt5/status');
}

export async function getMT5Symbols() {
  return apiFetch('/mt5/symbols');
}

export async function getMT5Tick(pair) {
  return apiFetch(`/mt5/tick/${pair}`);
}

export async function getMT5Positions() {
  return apiFetch('/mt5/positions');
}

export async function getMT5History() {
  return apiFetch('/mt5/history');
}

export async function getMT5Logs({ page = 1, pageSize = 20 } = {}) {
  const params = new URLSearchParams({ page, page_size: pageSize });
  return apiFetch(`/mt5/logs?${params}`);
}

export async function placeMT5DemoOrder(orderData) {
  return apiFetch('/mt5/demo-order', {
    method: 'POST',
    body: JSON.stringify(orderData),
  });
}

// ─── Telegram Alert API ───────────────────────────────────────────────────────

/** GET /alerts/telegram/status — returns config state, never returns token */
export async function getTelegramStatus() {
  return apiFetch('/alerts/telegram/status');
}

/**
 * POST /alerts/telegram/test — sends a test message.
 * Requires TELEGRAM_ALERTS_ENABLED=true on the backend; returns 422 otherwise.
 */
export async function sendTelegramTest() {
  return apiFetch('/alerts/telegram/test', { method: 'POST' });
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

/**
 * Estimate individual component scores from the live engine model text.
 * The GET /signal/{pair} response includes text descriptions but not raw scores,
 * so we reconstruct approximate values for the SignalCard factor bars.
 */
function _estimateScores(model = {}, newsStatus = 'CLEAR') {
  const m = model;

  const htf = (!m.higherTimeframeBias || m.higherTimeframeBias.includes('Neutral')) ? 5
    : (m.higherTimeframeBias.includes('HH/HL') || m.higherTimeframeBias.includes('LH/LL')) ? 15
    : 8;

  const liq = (!m.liquidity || m.liquidity.includes('No liquidity')) ? 0
    : 20; // any confirmed sweep is full score (partial handled by engine gate)

  const ms = m.structure?.includes('BOS confirmed') ? 20
    : m.structure?.includes('CHoCH confirmed') ? 15
    : 0;

  const fvg = (!m.fvg || m.fvg.includes('No Fair Value')) ? 0
    : m.fvg.includes('price in zone') ? 20
    : m.fvg.includes('approaching')   ? 15
    : 10; // detected but not retested

  const news    = newsStatus === 'CLEAR' ? 15 : 0;

  const session = (!m.session) ? 0
    : (m.session.includes('kill zone') || m.session.includes('Overlap')) ? 10
    : (m.session.includes('London') || m.session.includes('New York'))   ? 5
    : 0;

  return { htf, liq, ms, fvg, news, session };
}

/**
 * Convert live ICT engine response (GET /signal/{pair}) into the shape
 * expected by SignalCard (currentSignal) and TradePlanCard (tradePlan).
 */
function adaptLiveSignal(res) {
  const d = res?.data ?? {};
  const m = d.model ?? {};

  const dirMap = { BUY: 'BUY', SELL: 'SELL', WAIT: 'NEUTRAL' };
  const direction = dirMap[d.signal] ?? 'NEUTRAL';
  const scores    = _estimateScores(m, d.newsStatus);

  const factors = [
    { name: 'HTF Bias',         value: m.higherTimeframeBias ?? '—', score: scores.htf,     positive: scores.htf >= 8  },
    { name: 'Liquidity Sweep',  value: m.liquidity           ?? '—', score: scores.liq,     positive: scores.liq  > 0  },
    { name: 'Market Structure', value: m.structure           ?? '—', score: scores.ms,      positive: scores.ms   > 0  },
    { name: 'Fair Value Gap',   value: m.fvg                 ?? '—', score: scores.fvg,     positive: scores.fvg  > 0  },
    { name: 'News Risk',        value: d.newsStatus          ?? '—', score: scores.news,    positive: scores.news > 0   },
    { name: 'Session Timing',   value: m.session             ?? '—', score: scores.session, positive: scores.session > 0 },
  ];

  // Validity: 30 min from signal generation
  const validity = d.generatedAt
    ? new Date(new Date(d.generatedAt).getTime() + 30 * 60_000).toISOString()
    : new Date(Date.now() + 30 * 60_000).toISOString();

  // XAU/USD uses points, not pips. targetPips internally = 50 points.
  const targets = d.takeProfit != null
    ? [{ label: 'TP1', price: d.takeProfit, rr: d.rr ?? 0, pips: d.targetPips ?? 50, partial: 100 }]
    : [];

  const sessionLabel = m.session
    ? m.session.replace(' session', '').replace(' kill zone', ' KZ')
    : 'Unknown';

  return {
    currentSignal: {
      direction,
      strength:    d.qualityScore ?? 0,
      confidence:  d.qualityScore ?? 0,
      timestamp:   d.generatedAt ?? d.generated_at ?? new Date().toISOString(),
      session:     sessionLabel,
      timeframe:   'H4',
      price:       d.entry   ?? null,
      change:      null,
      changePct:   null,
      factors,
      // pass-throughs used by other components
      signal:      d.signal,
      reason:      d.reason,
      pair:        d.pair,
      displayPair: d.displayPair,
      newsStatus:  d.newsStatus,
      alertStatus: d.alertStatus,
      dataSource:  d.dataSource  ?? 'synthetic',
      weightsUsed: d.weightsUsed ?? null,
      sentiment:   d.model?.sentiment ?? null,
    },
    tradePlan: {
      entry:           d.entry,
      stopLoss:        d.stopLoss,
      stopLossPips:    d.riskPips,   // internally riskPips — displayed as points for XAU/USD
      stopLossPoints:  d.riskPips,   // alias with correct label
      targetPoints:    d.targetPips ?? 50,
      targets,
      riskPercent:     null,
      positionSize:    null,
      accountSize:     null,
      riskAmount:      null,
      validity,
      notes: d.signal === 'WAIT'
        ? (d.reason ?? 'Waiting for XAU/USD signal conditions to align.')
        : `XAU/USD ${d.signal} — ${d.reason ?? 'All gates passed.'}`,
    },
  };
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
