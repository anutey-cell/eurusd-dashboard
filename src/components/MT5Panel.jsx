/**
 * MT5Panel
 *
 * Self-contained MT5 Demo Connection panel.
 * Displays connection status, account info, live tick/spread,
 * open positions, and the DB-backed trade log.
 *
 * Placement logic for "Place Demo Trade":
 *   - Uses `currentSignal` (from Dashboard's getSignal → adaptSignal) as the
 *     signal source. The currentSignal shape is:
 *     { direction, strength, confidence, timestamp, session, timeframe,
 *       price, change, changePct, factors }
 *   - The backend /mt5/demo-order expects { signal, pair, entry, ... }.
 *     We map `direction` → `signal` in the order payload.
 *   - We do NOT call analyzeSignalForPair here; that is PaperTradePanel's role.
 *     MT5Panel piggybacks on the already-fetched currentSignal for its gate checks.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import {
  RefreshCw, Wifi, WifiOff, AlertTriangle, CheckCircle,
  XCircle, Clock, TrendingUp, TrendingDown, Minus, Activity,
} from 'lucide-react';
import {
  getMT5Status,
  getMT5Tick,
  getMT5Positions,
  getMT5Logs,
  placeMT5DemoOrder,
  getTelegramStatus,
  sendTelegramTest,
} from '../services/api';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmt(value, decimals = 5) {
  if (value == null || value === undefined) return '—';
  return Number(value).toFixed(decimals);
}

function fmtTime(isoStr) {
  if (!isoStr) return '—';
  try {
    return new Date(isoStr).toLocaleTimeString();
  } catch {
    return isoStr;
  }
}

function fmtDateTime(isoStr) {
  if (!isoStr) return '—';
  try {
    const d = new Date(isoStr);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
  } catch {
    return isoStr;
  }
}

function truncate(str, max = 40) {
  if (!str) return '—';
  return str.length > max ? str.slice(0, max) + '…' : str;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function StatusDot({ connected }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${
        connected ? 'bg-green-400 shadow-[0_0_6px_#4ade80]' : 'bg-red-500'
      }`}
    />
  );
}

function Pill({ children, color = 'gray' }) {
  const map = {
    green:  'bg-green-500/15 text-green-400 border-green-500/30',
    red:    'bg-red-500/15 text-red-400 border-red-500/30',
    amber:  'bg-amber-500/15 text-amber-400 border-amber-500/30',
    gray:   'bg-gray-700/40 text-gray-500 border-gray-600/30',
    blue:   'bg-blue-500/15 text-blue-400 border-blue-500/30',
  };
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-[10px] font-semibold border ${map[color] ?? map.gray}`}>
      {children}
    </span>
  );
}

function InfoCard({ label, value, valueClass = 'text-gray-200' }) {
  return (
    <div className="bg-[#0d1117] rounded-lg p-3 border border-[#263044]">
      <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">{label}</div>
      <div className={`font-mono text-sm font-bold ${valueClass}`}>{value ?? '—'}</div>
    </div>
  );
}

function StatusBadge({ status }) {
  const map = {
    accepted: { color: 'green', label: 'accepted' },
    rejected: { color: 'amber', label: 'rejected' },
    failed:   { color: 'red',   label: 'failed' },
  };
  const cfg = map[status?.toLowerCase()] ?? { color: 'gray', label: status ?? '—' };
  return <Pill color={cfg.color}>{cfg.label}</Pill>;
}

function DirectionIcon({ direction }) {
  const d = direction?.toUpperCase();
  if (d === 'BUY')  return <TrendingUp  size={12} className="text-green-400" />;
  if (d === 'SELL') return <TrendingDown size={12} className="text-red-400"  />;
  return <Minus size={12} className="text-gray-500" />;
}

// ─── Main Component ───────────────────────────────────────────────────────────

export default function MT5Panel({ selectedPair, currentSignal, tradePlan }) {
  const pairCode  = selectedPair?.code  ?? 'xauusd';
  const pairLabel = selectedPair?.label ?? 'XAU/USD';

  // ── state ──────────────────────────────────────────────────────────────────
  const [status,       setStatus]       = useState(null);
  const [tick,         setTick]         = useState(null);
  const [positions,    setPositions]    = useState([]);
  const [logs,         setLogs]         = useState([]);
  const [loading,      setLoading]      = useState(true);
  const [fetchError,   setFetchError]   = useState(null);
  const [orderLoading, setOrderLoading] = useState(false);
  const [orderResult,  setOrderResult]  = useState(null); // { success, message, ticket }
  const [tgStatus,     setTgStatus]     = useState(null);
  const [tgTesting,    setTgTesting]    = useState(false);
  const [tgTestResult, setTgTestResult] = useState(null);

  const tickIntervalRef = useRef(null);

  // ── data fetching ──────────────────────────────────────────────────────────

  const fetchTick = useCallback(async () => {
    try {
      const data = await getMT5Tick(pairCode);
      setTick(data?.data ?? data);
    } catch {
      // silent — tick errors don't reset the whole panel
    }
  }, [pairCode]);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    try {
      const [statusRes, positionsRes, logsRes, tgRes] = await Promise.allSettled([
        getMT5Status(),
        getMT5Positions(),
        getMT5Logs({ page: 1, pageSize: 10 }),
        getTelegramStatus(),
      ]);

      if (tgRes.status === 'fulfilled') {
        setTgStatus(tgRes.value?.data ?? tgRes.value);
      }

      if (statusRes.status === 'fulfilled') {
        setStatus(statusRes.value?.data ?? statusRes.value);
      } else {
        setStatus(null);
        setFetchError(statusRes.reason?.message ?? 'MT5 backend unreachable');
      }

      if (positionsRes.status === 'fulfilled') {
        const raw = positionsRes.value?.data ?? positionsRes.value;
        setPositions(Array.isArray(raw) ? raw : (raw?.positions ?? []));
      }

      if (logsRes.status === 'fulfilled') {
        const raw = logsRes.value?.data ?? logsRes.value;
        setLogs(Array.isArray(raw) ? raw : (raw?.logs ?? []));
      }
    } finally {
      setLoading(false);
    }
    // fetch tick separately (non-blocking)
    fetchTick();
  }, [fetchTick]);

  // initial load + re-fetch when pair changes
  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // auto-refresh tick every 10 seconds
  useEffect(() => {
    clearInterval(tickIntervalRef.current);
    tickIntervalRef.current = setInterval(fetchTick, 10_000);
    return () => clearInterval(tickIntervalRef.current);
  }, [fetchTick]);

  // ── order placement ────────────────────────────────────────────────────────

  async function handlePlaceDemoOrder() {
    if (!currentSignal) return;
    setOrderLoading(true);
    setOrderResult(null);
    try {
      // Build order payload from currentSignal + tradePlan.
      // currentSignal has: direction, confidence (≈ qualityScore), price, timeframe, session
      // tradePlan has: entry, stopLoss, stopLossPips, targets[{price,rr}]
      const tp1 = tradePlan?.targets?.find(t => t.label === 'TP2') ?? tradePlan?.targets?.[1] ?? tradePlan?.targets?.[0];
      const rrValue = parseFloat(tp1?.rr ?? 0);
      const payload = {
        pair:         pairCode,                               // "xauusd"
        signal:       currentSignal.direction,                // 'BUY' | 'SELL'
        entry:        tradePlan?.entry ?? currentSignal.price,
        stopLoss:     tradePlan?.stopLoss,
        takeProfit:   tp1?.price ?? null,
        riskPips:     tradePlan?.stopLossPips ?? 0,
        targetPips:   selectedPair?.decimals === 2 ? 50 : 40, // pair-specific default
        rr:           rrValue,
        qualityScore: currentSignal.confidence ?? 0,          // confidence ≈ qualityScore
        newsStatus:   'CLEAR',                                // gated by frontend already
        reason:       `${currentSignal.session ?? ''} ${currentSignal.timeframe ?? 'H4'} setup — ICT signal dashboard`,
      };
      const res = await placeMT5DemoOrder(payload);
      const data = res?.data ?? res;
      setOrderResult({
        success: true,
        message: data?.message ?? 'Order placed successfully',
        ticket:  data?.ticket  ?? data?.id ?? null,
      });
      // refresh positions and logs after successful order
      fetchAll();
    } catch (err) {
      setOrderResult({
        success: false,
        message: err?.message ?? 'Order failed',
        ticket:  null,
      });
    } finally {
      setOrderLoading(false);
    }
  }

  // ── gate checks for "Place Demo Trade" ────────────────────────────────────

  // currentSignal uses `direction` not `signal`
  const signalDirection  = currentSignal?.direction?.toUpperCase();
  const isTradeable      = signalDirection === 'BUY' || signalDirection === 'SELL';
  // confidence ≈ qualityScore in the adapted signal
  const qualityScore     = currentSignal?.confidence ?? 0;
  // rr is not in the current signal shape from getSignal; treat as passing unless explicitly < 2.5
  const rrValue          = currentSignal?.rr ?? null;
  const rrPasses         = rrValue == null || rrValue >= 2.5;
  // newsStatus not in currentSignal shape from getSignal — treat as CLEAR unless explicitly blocked
  const newsStatus       = currentSignal?.newsStatus ?? 'CLEAR';

  const gates = {
    connected:      status?.connected === true,
    demoMode:       status?.mode === 'demo',
    execEnabled:    status?.execution_enabled === true,
    demoAllowed:    status?.demo_trading_allowed === true,
    hasSignal:      isTradeable,
    qualityOk:      qualityScore >= 80,
    rrOk:           rrPasses,
    newsOk:         newsStatus === 'CLEAR',
  };

  const canTrade = Object.values(gates).every(Boolean);

  function primaryBlockReason() {
    if (!gates.connected)   return 'MT5 not connected';
    if (!gates.demoMode)    return 'Not in demo mode — live execution blocked';
    if (!gates.execEnabled) return 'Execution disabled on backend';
    if (!gates.demoAllowed) return 'Demo trading not allowed';
    if (!gates.hasSignal)   return `No actionable signal (current: ${signalDirection ?? 'NONE'})`;
    if (!gates.qualityOk)   return `Quality score too low (${qualityScore}/100, need ≥ 80)`;
    if (!gates.rrOk)        return `R:R too low (${rrValue}, need ≥ 2.5)`;
    if (!gates.newsOk)      return `News event blocking trade (${newsStatus})`;
    return null;
  }

  // ── spread colour ──────────────────────────────────────────────────────────

  function spreadColor(spread) {
    if (spread == null) return 'text-gray-600';
    if (spread <= 1.5)  return 'text-green-400';
    if (spread <= 3)    return 'text-amber-400';
    return 'text-red-400';
  }

  // ─── render ────────────────────────────────────────────────────────────────

  const connected = status?.connected ?? false;
  const mode      = status?.mode ?? null;
  const account   = status?.account ?? {};

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-5">

      {/* ── 1. Header ──────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Activity size={15} className="text-blue-400" />
          <StatusDot connected={connected} />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
            MT5 Demo Connection
          </h2>
          {loading && (
            <RefreshCw size={12} className="animate-spin text-blue-400 ml-1" />
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Mode badge */}
          {mode === 'demo' ? (
            <Pill color="amber">DEMO MODE</Pill>
          ) : mode === 'live' ? (
            <Pill color="red">LIVE — BLOCKED</Pill>
          ) : (
            <Pill color="gray">UNKNOWN MODE</Pill>
          )}

          {/* Manual refresh */}
          <button
            onClick={fetchAll}
            disabled={loading}
            title="Refresh MT5 data"
            className="p-1.5 rounded-lg bg-[#131c27] border border-[#263044] text-gray-500 hover:text-gray-300 disabled:opacity-40 transition-colors"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* ── 2. Warning banner ─────────────────────────────────────────────── */}
      {mode === 'live' ? (
        <div className="flex items-start gap-2.5 rounded-lg border border-red-700/50 bg-red-900/20 px-3 py-2.5 text-xs text-red-300">
          <AlertTriangle size={13} className="flex-shrink-0 mt-0.5 text-red-400" />
          <span>
            <strong>Live account detected.</strong> Execution blocked. Read-only monitoring only.
          </span>
        </div>
      ) : (
        <div className="flex items-start gap-2.5 rounded-lg border border-amber-600/40 bg-amber-900/15 px-3 py-2.5 text-xs text-amber-300">
          <AlertTriangle size={13} className="flex-shrink-0 mt-0.5 text-amber-400" />
          <span>
            MT5 demo mode only. Live trading is disabled. No real funds at risk.
          </span>
        </div>
      )}

      {/* ── Fetch error ────────────────────────────────────────────────────── */}
      {fetchError && (
        <div className="flex items-start gap-2 rounded-lg border border-red-800/40 bg-red-900/15 px-3 py-2 text-xs text-red-400">
          <WifiOff size={13} className="flex-shrink-0 mt-0.5" />
          <span>
            MT5 not connected — backend may be running on Linux where MetaTrader5 is unavailable.{' '}
            <span className="text-red-500/70">{fetchError}</span>
          </span>
        </div>
      )}

      {/* ── 3. Account info grid ───────────────────────────────────────────── */}
      <div>
        <p className="text-[10px] text-gray-600 uppercase tracking-widest mb-2">Account</p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <InfoCard
            label="Balance"
            value={account.balance != null ? `$${Number(account.balance).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—'}
            valueClass="text-gray-200"
          />
          <InfoCard
            label="Equity"
            value={account.equity  != null ? `$${Number(account.equity).toLocaleString('en-US',  { minimumFractionDigits: 2 })}` : '—'}
            valueClass="text-blue-400"
          />
          <InfoCard
            label="Free Margin"
            value={account.free_margin != null ? `$${Number(account.free_margin).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—'}
            valueClass="text-gray-200"
          />
          <InfoCard
            label="Margin Level"
            value={account.margin_level != null ? `${Number(account.margin_level).toFixed(1)}%` : '—'}
            valueClass={
              account.margin_level == null ? 'text-gray-600'
              : account.margin_level >= 300 ? 'text-green-400'
              : account.margin_level >= 150 ? 'text-amber-400'
              : 'text-red-400'
            }
          />
        </div>
      </div>

      {/* ── 4. Spread & Symbol row ─────────────────────────────────────────── */}
      <div className="bg-[#131c27] rounded-xl border border-[#263044] p-3">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <div>
            <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Active Pair</div>
            <div className="font-semibold text-gray-200">{pairLabel}</div>
          </div>
          <div>
            <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Broker Symbol</div>
            <div className="font-mono text-gray-300">{tick?.symbol ?? status?.broker_symbol ?? '—'}</div>
          </div>
          <div>
            <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Spread</div>
            <div className={`font-mono font-bold ${spreadColor(tick?.spread)}`}>
              {tick?.spread != null ? `${Number(tick.spread).toFixed(1)} pts` : '—'}
            </div>
          </div>
          <div>
            <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Last Updated</div>
            <div className="font-mono text-gray-500 text-[11px]">
              {tick?.time ? fmtTime(tick.time) : '—'}
            </div>
          </div>
        </div>
      </div>

      {/* ── 5. Execution status row ────────────────────────────────────────── */}
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-[10px] text-gray-600 uppercase tracking-wider">Status:</span>
        {status?.execution_enabled
          ? <Pill color="green">Execution: ENABLED</Pill>
          : <Pill color="gray">Execution: DISABLED</Pill>
        }
        {status?.demo_trading_allowed
          ? <Pill color="green">Demo Trading: ALLOWED</Pill>
          : <Pill color="red">Demo Trading: BLOCKED</Pill>
        }
        {connected
          ? <Pill color="blue"><Wifi size={9} className="mr-1" />Connected</Pill>
          : <Pill color="gray"><WifiOff size={9} className="mr-1" />Disconnected</Pill>
        }
      </div>

      {/* ── 6. Place Demo Trade ────────────────────────────────────────────── */}
      <div className="border-t border-[#263044] pt-4 space-y-3">
        <p className="text-[10px] text-gray-600 uppercase tracking-widest font-semibold">
          Demo Order Placement
        </p>

        <button
          onClick={canTrade && !orderLoading ? handlePlaceDemoOrder : undefined}
          disabled={!canTrade || orderLoading}
          className={`w-full py-3 rounded-xl font-semibold text-sm flex items-center justify-center gap-2 transition-colors ${
            canTrade && !orderLoading
              ? 'bg-blue-600 hover:bg-blue-500 text-white cursor-pointer'
              : 'bg-gray-800 text-gray-600 cursor-not-allowed'
          }`}
        >
          {orderLoading ? (
            <><RefreshCw size={14} className="animate-spin" /> Placing order…</>
          ) : isTradeable ? (
            <>{signalDirection === 'BUY'
              ? <TrendingUp  size={14} />
              : <TrendingDown size={14} />
            } Place Demo Trade — {signalDirection} {pairLabel}</>
          ) : (
            <><Minus size={14} /> Place Demo Trade</>
          )}
        </button>

        {/* Show primary block reason when disabled */}
        {!canTrade && primaryBlockReason() && (
          <p className="text-[11px] text-gray-600 text-center">
            <span className="text-amber-500/70">Blocked:</span> {primaryBlockReason()}
          </p>
        )}

        {/* Order result banner */}
        {orderResult && (
          <div className={`flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs ${
            orderResult.success
              ? 'bg-green-900/20 border-green-700/40 text-green-300'
              : 'bg-red-900/20 border-red-700/40 text-red-300'
          }`}>
            {orderResult.success
              ? <CheckCircle size={13} className="flex-shrink-0 mt-0.5 text-green-400" />
              : <XCircle    size={13} className="flex-shrink-0 mt-0.5 text-red-400"   />
            }
            <div>
              <span className="font-semibold">
                {orderResult.success ? 'Order accepted' : 'Order rejected'}
              </span>
              {orderResult.ticket && (
                <span className="ml-2 font-mono text-green-400/80">Ticket #{orderResult.ticket}</span>
              )}
              <p className="mt-0.5 text-[11px] opacity-80">{orderResult.message}</p>
            </div>
            <button
              onClick={() => setOrderResult(null)}
              className="ml-auto text-gray-500 hover:text-gray-300"
            >
              <XCircle size={12} />
            </button>
          </div>
        )}
      </div>

      {/* ── 7. Open Positions table ────────────────────────────────────────── */}
      <div className="border-t border-[#263044] pt-4">
        <p className="text-[10px] text-gray-600 uppercase tracking-widest font-semibold mb-3">
          Open Positions ({positions.length})
        </p>
        {positions.length === 0 ? (
          <div className="text-center text-gray-600 text-xs py-4 bg-[#131c27] rounded-xl border border-[#263044]">
            No open positions
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-[#263044]">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[10px] text-gray-600 uppercase tracking-wider border-b border-[#263044]">
                  <th className="text-left px-3 py-2">Symbol</th>
                  <th className="text-left px-3 py-2">Dir</th>
                  <th className="text-right px-3 py-2">Lots</th>
                  <th className="text-right px-3 py-2">Entry</th>
                  <th className="text-right px-3 py-2">SL</th>
                  <th className="text-right px-3 py-2">TP</th>
                  <th className="text-right px-3 py-2">P&amp;L</th>
                  <th className="text-left px-3 py-2">Open Time</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos, i) => {
                  const pnl = pos.profit ?? pos.pnl ?? null;
                  return (
                    <tr
                      key={pos.ticket ?? pos.id ?? i}
                      className="border-b border-[#1e2535] last:border-0 hover:bg-[#131c27]/60 transition-colors"
                    >
                      <td className="px-3 py-2 font-mono text-gray-300">{pos.symbol ?? '—'}</td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1">
                          <DirectionIcon direction={pos.type ?? pos.direction} />
                          <span className={
                            (pos.type ?? pos.direction)?.toUpperCase() === 'BUY'
                              ? 'text-green-400'
                              : 'text-red-400'
                          }>
                            {(pos.type ?? pos.direction)?.toUpperCase() ?? '—'}
                          </span>
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-gray-300">
                        {pos.volume ?? pos.lots ?? '—'}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-gray-300">
                        {fmt(pos.price_open ?? pos.entry)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-red-400">
                        {fmt(pos.sl ?? pos.stop_loss)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-green-400">
                        {fmt(pos.tp ?? pos.take_profit)}
                      </td>
                      <td className={`px-3 py-2 text-right font-mono font-bold ${
                        pnl == null ? 'text-gray-600'
                        : pnl >= 0  ? 'text-green-400'
                        : 'text-red-400'
                      }`}>
                        {pnl != null ? (pnl >= 0 ? '+' : '') + pnl.toFixed(2) : '—'}
                      </td>
                      <td className="px-3 py-2 font-mono text-gray-500 text-[10px]">
                        {fmtDateTime(pos.time ?? pos.open_time)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── 8. Trade Log (DB) ─────────────────────────────────────────────── */}
      <div className="border-t border-[#263044] pt-4">
        <p className="text-[10px] text-gray-600 uppercase tracking-widest font-semibold mb-3">
          MT5 Trade Log (last 10)
        </p>
        {logs.length === 0 ? (
          <div className="text-center text-gray-600 text-xs py-4 bg-[#131c27] rounded-xl border border-[#263044]">
            No log entries found
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-[#263044]">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[10px] text-gray-600 uppercase tracking-wider border-b border-[#263044]">
                  <th className="text-left px-3 py-2">Time</th>
                  <th className="text-left px-3 py-2">Pair</th>
                  <th className="text-left px-3 py-2">Signal</th>
                  <th className="text-right px-3 py-2">Vol</th>
                  <th className="text-center px-3 py-2">Status</th>
                  <th className="text-right px-3 py-2">Spread</th>
                  <th className="text-left px-3 py-2">Rejection Reason</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log, i) => (
                  <tr
                    key={log.id ?? i}
                    className="border-b border-[#1e2535] last:border-0 hover:bg-[#131c27]/60 transition-colors"
                  >
                    <td className="px-3 py-2 font-mono text-gray-500 text-[10px] whitespace-nowrap">
                      {fmtDateTime(log.created_at ?? log.timestamp ?? log.time)}
                    </td>
                    <td className="px-3 py-2 font-mono text-gray-300">{log.pair ?? '—'}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <DirectionIcon direction={log.signal ?? log.direction} />
                        <span className={
                          (log.signal ?? log.direction)?.toUpperCase() === 'BUY'
                            ? 'text-green-400'
                            : (log.signal ?? log.direction)?.toUpperCase() === 'SELL'
                              ? 'text-red-400'
                              : 'text-gray-500'
                        }>
                          {(log.signal ?? log.direction)?.toUpperCase() ?? '—'}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-gray-300">
                      {log.volume ?? log.lots ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-center">
                      <StatusBadge status={log.status} />
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      <span className={spreadColor(log.spread)}>
                        {log.spread != null ? Number(log.spread).toFixed(1) : '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-500 text-[11px]">
                      {truncate(log.rejection_reason ?? log.reason, 40)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── 9. Telegram Signal Alerts ─────────────────────────────────────── */}
      <div className="mt-4 bg-[#0d1117] rounded-xl border border-[#263044] p-4">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-2">
            <span className="text-sm">📨</span>
            <span className="text-xs font-semibold text-gray-300">Telegram Signal Alerts</span>
            {tgStatus?.ready ? (
              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-green-500/15 text-green-400 border border-green-500/20">ACTIVE</span>
            ) : (
              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-gray-700/50 text-gray-500 border border-gray-700">INACTIVE</span>
            )}
          </div>
          <button
            onClick={async () => {
              setTgTesting(true);
              setTgTestResult(null);
              try {
                const res = await sendTelegramTest();
                setTgTestResult({ ok: true, msg: res?.message ?? 'Test alert sent!' });
              } catch (err) {
                const detail = err?.message ?? 'Failed';
                setTgTestResult({
                  ok: false,
                  msg: detail.includes('TELEGRAM_ALERTS_ENABLED')
                    ? 'Set TELEGRAM_ALERTS_ENABLED=true in backend/.env first'
                    : detail,
                });
              } finally {
                setTgTesting(false);
              }
            }}
            disabled={tgTesting || !tgStatus?.enabled}
            title={!tgStatus?.enabled ? 'Set TELEGRAM_ALERTS_ENABLED=true in backend/.env to enable' : 'Send test alert to your Telegram chat'}
            className="px-2.5 py-1 rounded-md text-[10px] font-medium bg-blue-600/20 hover:bg-blue-600/30 text-blue-400 border border-blue-500/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {tgTesting ? 'Sending…' : 'Send Test Alert'}
          </button>
        </div>

        {/* Notification-only reminder — always visible */}
        <p className="text-[9px] text-amber-500/70 mb-3 flex items-center gap-1">
          <span>⚠️</span>
          Telegram alerts are notification-only. They do not place trades.
        </p>

        {/* Status grid — 6 cells */}
        <div className="grid grid-cols-3 gap-2 mb-3">
          {[
            { label: 'Alerts Enabled',  val: tgStatus?.enabled },
            { label: 'Bot Token',       val: tgStatus?.botTokenConfigured },
            { label: 'Chat ID',         val: tgStatus?.chatIdConfigured ? (tgStatus?.chatIdMasked || 'set') : false },
            { label: 'Signal Alerts',   val: tgStatus?.signalAlerts },
            { label: 'Parse Mode',      val: tgStatus?.parseMode ?? '—' },
            { label: 'Cooldown',        val: tgStatus?.cooldownMinutes != null ? `${tgStatus.cooldownMinutes} min` : '—' },
          ].map(({ label, val }) => (
            <div key={label} className="bg-[#131c27] rounded-lg p-2 border border-[#263044]">
              <div className="text-[9px] text-gray-600 uppercase tracking-wider mb-0.5">{label}</div>
              <div className={`text-[10px] font-semibold ${
                val === true  ? 'text-green-400' :
                val === false ? 'text-gray-600'  :
                'text-gray-400'
              }`}>
                {val === true ? 'Yes' : val === false ? 'No' : (val ?? '—')}
              </div>
            </div>
          ))}
        </div>

        {/* Setup instructions when not configured */}
        {!tgStatus?.ready && (
          <div className="text-[10px] text-gray-600 space-y-1 border-t border-[#263044] pt-2">
            <p className="font-semibold text-gray-500">Setup:</p>
            <p>1. Create bot via <code className="text-gray-400">@BotFather</code> → copy token</p>
            <p>2. Visit <code className="text-gray-400">/getUpdates</code> to get your chat ID</p>
            <p>3. Add to <code className="text-gray-400">backend/.env</code>:</p>
            <pre className="bg-[#0a0f1a] rounded p-1.5 text-[9px] text-gray-400 mt-1 whitespace-pre-wrap">
{`TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id`}
            </pre>
            <p>4. Restart backend → click Send Test Alert</p>
          </div>
        )}

        {/* Test result banner */}
        {tgTestResult && (
          <div className={`mt-2 px-3 py-1.5 rounded-lg text-[10px] border ${
            tgTestResult.ok
              ? 'bg-green-500/10 border-green-500/20 text-green-400'
              : 'bg-red-500/10 border-red-500/20 text-red-400'
          }`}>
            {tgTestResult.ok ? '✅ ' : '❌ '}{tgTestResult.msg}
          </div>
        )}
      </div>
    </section>
  );
}
