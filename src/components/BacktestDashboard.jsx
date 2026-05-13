import { useState } from 'react';
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Cell,
} from 'recharts';
import { Play, AlertTriangle, ChevronDown, ChevronUp, TrendingUp, TrendingDown } from 'lucide-react';
import { runBacktest } from '../services/api';

// ─── Constants ────────────────────────────────────────────────────────────────

const TIMEFRAMES = ['M15', 'M30', 'H1', 'H4', 'D1'];
const LOOKBACKS  = [
  { label: '100 candles',  value: 100 },
  { label: '200 candles',  value: 200 },
  { label: '500 candles',  value: 500 },
  { label: '1 000 candles', value: 1000 },
  { label: '2 000 candles', value: 2000 },
  { label: '5 000 candles', value: 5000 },
];

// ─── Sub-components ───────────────────────────────────────────────────────────

function Stat({ label, value, sub, color = 'text-white', small = false }) {
  return (
    <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3 flex flex-col gap-0.5">
      <span className="text-[10px] text-gray-500 uppercase tracking-wider">{label}</span>
      <span className={`font-mono font-bold ${small ? 'text-base' : 'text-lg'} ${color}`}>{value}</span>
      {sub && <span className="text-[10px] text-gray-600">{sub}</span>}
    </div>
  );
}

function SignalBadge({ signal }) {
  return (
    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded font-mono ${
      signal === 'BUY'  ? 'bg-emerald-500/20 text-emerald-400' :
      signal === 'SELL' ? 'bg-red-500/20 text-red-400' :
                          'bg-gray-500/20 text-gray-400'
    }`}>{signal}</span>
  );
}

function ResultBadge({ result }) {
  return (
    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
      result === 'WIN'  ? 'bg-emerald-500/20 text-emerald-400' :
      result === 'LOSS' ? 'bg-red-500/20 text-red-400' :
                          'bg-gray-500/20 text-gray-400'
    }`}>{result}</span>
  );
}

const CustomEquityTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="bg-[#1a2035] border border-[#263044] rounded p-2 text-xs">
      <div className="text-gray-400">Trade #{d.trade}</div>
      <div className={`font-mono font-bold ${d.pips >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
        {d.pips >= 0 ? '+' : ''}{d.pips} pips
      </div>
    </div>
  );
};

const CustomSessionTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-[#1a2035] border border-[#263044] rounded p-2 text-xs">
      <div className="text-gray-300 font-medium mb-1">{label}</div>
      {payload.map(p => (
        <div key={p.dataKey} className="text-gray-400">
          {p.name}: <span className="font-mono text-white">{p.value}</span>
        </div>
      ))}
    </div>
  );
};

// ─── Trade List ───────────────────────────────────────────────────────────────

function TradeList({ trades }) {
  const [page, setPage]     = useState(0);
  const [expanded, setExpanded] = useState(false);
  const PAGE = 15;

  if (!expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        className="w-full text-xs text-gray-500 hover:text-gray-300 flex items-center justify-center gap-1.5 py-2 border border-dashed border-[#263044] rounded-lg transition-colors"
      >
        <ChevronDown size={13} />
        Show {trades.length} individual trades
      </button>
    );
  }

  const total  = trades.length;
  const pages  = Math.ceil(total / PAGE);
  const slice  = trades.slice(page * PAGE, (page + 1) * PAGE);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-400 font-medium">Individual Trades ({total})</span>
        <button
          onClick={() => setExpanded(false)}
          className="text-[10px] text-gray-600 hover:text-gray-400 flex items-center gap-1"
        >
          <ChevronUp size={11} /> Collapse
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="text-gray-500 text-[10px] uppercase tracking-wider">
              {['Time', 'Signal', 'Entry', 'SL', 'TP', 'Result', 'Pips', 'RR', 'Session', 'Score'].map(h => (
                <th key={h} className="text-left py-1.5 px-2 border-b border-[#263044]">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.map((t, i) => (
              <tr
                key={i}
                className="border-b border-[#1a2035] hover:bg-[#161b27] transition-colors"
                title={t.reason}
              >
                <td className="py-1.5 px-2 font-mono text-gray-400 whitespace-nowrap">
                  {t.time ? t.time.slice(0, 16).replace('T', ' ') : '—'}
                </td>
                <td className="py-1.5 px-2"><SignalBadge signal={t.signal} /></td>
                <td className="py-1.5 px-2 font-mono text-gray-300">{t.entry?.toFixed(5)}</td>
                <td className="py-1.5 px-2 font-mono text-red-400/70">{t.stopLoss?.toFixed(5)}</td>
                <td className="py-1.5 px-2 font-mono text-emerald-400/70">{t.takeProfit?.toFixed(5)}</td>
                <td className="py-1.5 px-2"><ResultBadge result={t.result} /></td>
                <td className={`py-1.5 px-2 font-mono font-bold ${t.pips > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {t.pips > 0 ? '+' : ''}{t.pips}
                </td>
                <td className="py-1.5 px-2 font-mono text-gray-400">{t.rr?.toFixed(2)}</td>
                <td className="py-1.5 px-2 text-gray-400">{t.session}</td>
                <td className="py-1.5 px-2 font-mono text-gray-500">{t.qualityScore}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="flex items-center justify-between text-xs text-gray-500 pt-1">
          <button
            disabled={page === 0}
            onClick={() => setPage(p => p - 1)}
            className="px-3 py-1 border border-[#263044] rounded disabled:opacity-30 hover:text-gray-300"
          >
            Prev
          </button>
          <span className="font-mono">Page {page + 1} / {pages}</span>
          <button
            disabled={page === pages - 1}
            onClick={() => setPage(p => p + 1)}
            className="px-3 py-1 border border-[#263044] rounded disabled:opacity-30 hover:text-gray-300"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Results Panel ────────────────────────────────────────────────────────────

function Results({ data, timeframe, lookback }) {
  const {
    totalTrades, wins, losses, winRate, averageRR, averagePips,
    expectancy, profitFactor, maxDrawdown, totalPips,
    bestSession, worstSession, buyWinRate, sellWinRate,
    sessionBreakdown = [], equityCurve = [], trades = [],
    spreadPips, slippagePips,
  } = data;

  const maxWinRate = sessionBreakdown.length
    ? Math.max(...sessionBreakdown.map(s => s.winRate))
    : 0;

  const sessionColors = sessionBreakdown.map(s =>
    s.winRate === maxWinRate ? '#10b981' :
    s.session === worstSession ? '#ef4444' : '#3b82f6'
  );

  return (
    <div className="space-y-4">

      {/* Disclaimer */}
      <div className="flex items-start gap-2 bg-amber-500/8 border border-amber-500/20 rounded-lg px-3 py-2.5">
        <AlertTriangle size={13} className="text-amber-400 flex-shrink-0 mt-0.5" />
        <p className="text-[11px] text-amber-300/80 leading-relaxed">
          <span className="font-semibold text-amber-300">Backtest results are indicative only.</span>{' '}
          They do not guarantee live performance. Past results over {totalTrades} trades on {timeframe} {lookback}-candle
          data include {spreadPips}pip spread and {slippagePips}pip slippage. Live conditions will differ.
        </p>
      </div>

      {/* KPI grid */}
      <div className="grid grid-cols-4 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        <Stat label="Total Trades"  value={totalTrades} />
        <Stat
          label="Win Rate"
          value={`${winRate}%`}
          color={winRate >= 55 ? 'text-emerald-400' : winRate >= 45 ? 'text-amber-400' : 'text-red-400'}
        />
        <Stat
          label="Expectancy"
          value={`${expectancy > 0 ? '+' : ''}${expectancy} pips`}
          color={expectancy > 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <Stat
          label="Profit Factor"
          value={profitFactor ? profitFactor.toFixed(2) : 'N/A'}
          color={profitFactor && profitFactor >= 1.5 ? 'text-emerald-400' : profitFactor && profitFactor >= 1 ? 'text-amber-400' : 'text-red-400'}
        />
        <Stat
          label="Max Drawdown"
          value={`${maxDrawdown} pips`}
          color="text-red-400"
        />
        <Stat
          label="Total Pips"
          value={`${totalPips > 0 ? '+' : ''}${totalPips}`}
          color={totalPips > 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <Stat label="Avg RR"    value={`1:${averageRR}`} />
        <Stat
          label="Avg Pips/Trade"
          value={`${averagePips > 0 ? '+' : ''}${averagePips}`}
          color={averagePips > 0 ? 'text-emerald-400' : 'text-red-400'}
        />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

        {/* Equity curve */}
        <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-gray-300">Cumulative Pips Curve</span>
            <span className={`text-xs font-mono ${totalPips >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {totalPips >= 0 ? '+' : ''}{totalPips} pips
            </span>
          </div>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={equityCurve} margin={{ top: 4, right: 4, bottom: 0, left: -10 }}>
              <defs>
                <linearGradient id="btGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={totalPips >= 0 ? '#10b981' : '#ef4444'} stopOpacity={0.25} />
                  <stop offset="95%" stopColor={totalPips >= 0 ? '#10b981' : '#ef4444'} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#263044" />
              <XAxis dataKey="trade" tick={{ fill: '#6b7280', fontSize: 9 }} />
              <YAxis tick={{ fill: '#6b7280', fontSize: 9 }} />
              <Tooltip content={<CustomEquityTooltip />} />
              <ReferenceLine y={0} stroke="#374151" strokeDasharray="4 2" />
              <Area
                type="monotone"
                dataKey="pips"
                stroke={totalPips >= 0 ? '#10b981' : '#ef4444'}
                fill="url(#btGrad)"
                strokeWidth={1.5}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Session performance */}
        <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-gray-300">Session Performance</span>
            <div className="flex items-center gap-3 text-[10px]">
              <span className="flex items-center gap-1 text-emerald-400">
                <TrendingUp size={10} /> Best: {bestSession}
              </span>
              <span className="flex items-center gap-1 text-red-400">
                <TrendingDown size={10} /> Worst: {worstSession}
              </span>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={sessionBreakdown} margin={{ top: 4, right: 4, bottom: 0, left: -10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#263044" />
              <XAxis dataKey="session" tick={{ fill: '#6b7280', fontSize: 9 }} />
              <YAxis domain={[0, 100]} tick={{ fill: '#6b7280', fontSize: 9 }} unit="%" />
              <Tooltip content={<CustomSessionTooltip />} />
              <Bar dataKey="winRate" name="Win Rate" radius={[3, 3, 0, 0]}>
                {sessionBreakdown.map((_, i) => (
                  <Cell key={i} fill={sessionColors[i]} fillOpacity={0.8} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Directional + W/L row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Stat
          label="BUY Win Rate"
          value={`${buyWinRate}%`}
          color={buyWinRate >= 55 ? 'text-emerald-400' : 'text-amber-400'}
          sub={`${trades.filter(t => t.signal === 'BUY').length} trades`}
        />
        <Stat
          label="SELL Win Rate"
          value={`${sellWinRate}%`}
          color={sellWinRate >= 55 ? 'text-emerald-400' : 'text-amber-400'}
          sub={`${trades.filter(t => t.signal === 'SELL').length} trades`}
        />
        <Stat label="Wins"   value={wins}   color="text-emerald-400" small />
        <Stat label="Losses" value={losses} color="text-red-400" small />
      </div>

      {/* Trade list */}
      {trades.length > 0 && <TradeList trades={trades} />}
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function BacktestDashboard() {
  const [timeframe, setTimeframe] = useState('H4');
  const [lookback,  setLookback]  = useState(500);
  const [loading,   setLoading]   = useState(false);
  const [result,    setResult]    = useState(null);
  const [error,     setError]     = useState(null);

  async function handleRun() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await runBacktest({ timeframe, lookback });
      setResult(data);
    } catch (err) {
      setError(err?.message ?? 'Backtest failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl p-4 space-y-4">

      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-sm font-semibold text-white">Backtesting Module</h2>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Walk-forward ICT signal simulation · No look-ahead bias · Spread &amp; slippage applied
          </p>
        </div>

        {/* Controls */}
        <div className="flex items-center gap-2 flex-wrap">
          <select
            value={timeframe}
            onChange={e => setTimeframe(e.target.value)}
            className="bg-[#161b27] border border-[#263044] text-gray-300 text-xs rounded px-2 py-1.5 focus:outline-none focus:border-blue-500"
          >
            {TIMEFRAMES.map(tf => (
              <option key={tf} value={tf}>{tf}</option>
            ))}
          </select>

          <select
            value={lookback}
            onChange={e => setLookback(Number(e.target.value))}
            className="bg-[#161b27] border border-[#263044] text-gray-300 text-xs rounded px-2 py-1.5 focus:outline-none focus:border-blue-500"
          >
            {LOOKBACKS.map(l => (
              <option key={l.value} value={l.value}>{l.label}</option>
            ))}
          </select>

          <button
            onClick={handleRun}
            disabled={loading}
            className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-500 disabled:bg-blue-800 disabled:cursor-not-allowed text-white text-xs font-medium px-4 py-1.5 rounded transition-colors"
          >
            <Play size={12} className={loading ? 'animate-pulse' : ''} />
            {loading ? 'Running…' : 'Run Backtest'}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="flex items-start gap-2 bg-red-500/10 border border-red-500/25 rounded-lg px-3 py-2.5 text-xs text-red-400">
          <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Empty state */}
      {!result && !loading && !error && (
        <div className="text-center py-10 text-gray-600 text-xs border border-dashed border-[#263044] rounded-lg">
          Select a timeframe and lookback, then click <span className="text-gray-400">Run Backtest</span> to simulate the signal engine on historical data.
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="text-center py-10 text-gray-500 text-xs">
          <div className="inline-flex items-center gap-2">
            <span className="w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
            Running {lookback}-candle {timeframe} backtest…
          </div>
          <p className="mt-2 text-gray-600">Large lookbacks may take several seconds.</p>
        </div>
      )}

      {/* Results */}
      {result && !loading && (
        <Results data={result} timeframe={timeframe} lookback={lookback} />
      )}
    </section>
  );
}
