import {
  AreaChart, Area, BarChart, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts';
import { Activity, RefreshCw, AlertTriangle, TrendingUp, TrendingDown } from 'lucide-react';
import { mockFullAnalytics } from '../data/mockData';
import { SkeletonCard } from './Skeleton';
import { FallbackChip } from './BackendBanner';

// ── Tooltip ───────────────────────────────────────────────────────────────────

const ChartTooltip = ({ active, payload, label, unit = '' }) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-[#1e2535] border border-[#263044] rounded-lg px-3 py-2 text-xs shadow-xl">
      <div className="text-gray-400 mb-1">{label}</div>
      {payload.map(p => (
        <div key={p.name} style={{ color: p.color ?? p.fill }} className="font-mono font-semibold">
          {typeof p.value === 'number' && p.value > 0 ? '+' : ''}{p.value}{unit}
        </div>
      ))}
    </div>
  );
};

// ── Sample confidence banner ──────────────────────────────────────────────────

const SAMPLE_CFG = {
  low:        { bg: 'bg-red-500/10 border-red-500/20',    text: 'text-red-400',    icon: AlertTriangle },
  developing: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-300',  icon: AlertTriangle },
  useful:     { bg: 'bg-blue-500/10 border-blue-500/20',   text: 'text-blue-300',   icon: Activity },
  strong:     { bg: 'bg-emerald-500/10 border-emerald-500/20', text: 'text-emerald-300', icon: Activity },
};

function SampleBanner({ info }) {
  if (!info) return null;
  const cfg  = SAMPLE_CFG[info.size] ?? SAMPLE_CFG.low;
  const Icon = cfg.icon;
  return (
    <div className={`flex items-start gap-2 border rounded-lg px-3 py-2 ${cfg.bg}`}>
      <Icon size={12} className={`${cfg.text} flex-shrink-0 mt-0.5`} />
      <span className={`text-[11px] ${cfg.text}`}>{info.message}</span>
    </div>
  );
}

// ── KPI card ──────────────────────────────────────────────────────────────────

function KPI({ label, value, sub, cls = 'text-white' }) {
  return (
    <div className="bg-[#1e2535] rounded-lg p-3">
      <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">{label}</div>
      <div className={`text-xl font-black font-mono ${cls}`}>{value ?? '—'}</div>
      {sub && <div className="text-[10px] text-gray-600 mt-0.5">{sub}</div>}
    </div>
  );
}

// ── Section header ────────────────────────────────────────────────────────────

function SectionHeader({ icon: Icon, label, color = 'text-blue-400' }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <Icon size={12} className={color} />
      <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">{label}</span>
    </div>
  );
}

// ── Directional comparison row ────────────────────────────────────────────────

function DualStat({ labelA, valA, clsA, labelB, valB, clsB }) {
  return (
    <div className="grid grid-cols-2 gap-2">
      <div className="bg-[#161b27] rounded-lg p-2.5 text-center">
        <div className="text-[10px] text-gray-500 mb-1">{labelA}</div>
        <div className={`text-lg font-black font-mono ${clsA}`}>{valA ?? '—'}</div>
      </div>
      <div className="bg-[#161b27] rounded-lg p-2.5 text-center">
        <div className="text-[10px] text-gray-500 mb-1">{labelB}</div>
        <div className={`text-lg font-black font-mono ${clsB}`}>{valB ?? '—'}</div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function AnalyticsDashboard({
  loading         = false,
  error           = null,
  isUsingFallback = false,
  refetch         = null,
  analytics       = mockFullAnalytics,
}) {
  const a = analytics ?? mockFullAnalytics;

  const fmt     = (v, decimals = 1) => v != null ? Number(v).toFixed(decimals) : null;
  const pct     = v => v != null ? `${fmt(v)}%` : null;
  const pips    = v => v != null ? `${v > 0 ? '+' : ''}${v} pips` : null;

  // Colour equity curve by direction (positive = green, negative = red)
  const equityPositive = a.equityCurve?.filter(p => p.cumulativePips >= 0) ?? [];

  // Session bar colors
  const sessionColor = (s) => {
    if (s.session === a.bestSession)  return '#10b981';
    if (s.session === a.worstSession) return '#ef4444';
    return '#3b82f6';
  };

  return (
    <div className="card">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <Activity size={13} className="text-blue-400" />
          <span className="card-title">Analytics</span>
          <FallbackChip isUsingFallback={isUsingFallback} error={error} />
        </div>
        <div className="flex items-center gap-2">
          {refetch && (
            <button onClick={refetch} title="Refresh"
              className="text-gray-600 hover:text-gray-400 transition-colors">
              <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
            </button>
          )}
          <span className="text-[10px] text-gray-600">
            {a.completedTrades} completed trades
          </span>
        </div>
      </div>

      {loading ? <SkeletonCard rows={10} /> : (
        <div className="p-4 space-y-5">

          {/* Sample confidence */}
          <SampleBanner info={a.sampleInfo} />

          {/* ── Core KPIs ────────────────────────────────────────────── */}
          <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-2">
            <KPI label="Win Rate"     value={pct(a.winRate)}      cls="text-emerald-400" />
            <KPI label="Expectancy"   value={pips(a.expectancy != null ? Math.round(a.expectancy) : null)}
                 sub="per trade"      cls={a.expectancy > 0 ? 'text-emerald-400' : 'text-red-400'} />
            <KPI label="Avg R:R"      value={a.averageRR != null ? `1:${fmt(a.averageRR, 2)}` : null}
                 cls="text-blue-400" />
            <KPI label="Profit Factor" value={fmt(a.profitFactor, 2)} cls="text-blue-400" />
            <KPI label="Max Drawdown" value={pips(a.maxDrawdown)} cls="text-red-400" />
            <KPI label="Avg Pips"     value={pips(a.averagePips != null ? Math.round(a.averagePips) : null)}
                 sub={`${a.completedTrades} trades`}
                 cls={a.averagePips > 0 ? 'text-emerald-400' : 'text-red-400'} />
          </div>

          {/* ── Supporting KPIs ──────────────────────────────────────── */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <KPI label="Avg Win"    value={a.averageWinPips  != null ? `+${Math.round(a.averageWinPips)} pips`  : null} cls="text-emerald-400" />
            <KPI label="Avg Loss"   value={a.averageLossPips != null ? `-${Math.round(a.averageLossPips)} pips` : null} cls="text-red-400" />
            <KPI label="Max W-Streak" value={a.consecutiveWins   ?? '—'} sub="consecutive wins"  cls="text-emerald-400" />
            <KPI label="Max L-Streak" value={a.consecutiveLosses ?? '—'} sub="consecutive losses" cls="text-red-400" />
          </div>

          {/* ── Charts row ───────────────────────────────────────────── */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

            {/* Equity curve (cumulative pips) */}
            <div className="bg-[#1e2535] rounded-xl p-4">
              <SectionHeader icon={TrendingUp} label="Equity Curve — Cumulative Pips" />
              {a.equityCurve?.length > 0 ? (
                <ResponsiveContainer width="100%" height={180}>
                  <AreaChart data={a.equityCurve} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                    <defs>
                      <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%"  stopColor="#10b981" stopOpacity={0.25} />
                        <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#263044" />
                    <XAxis dataKey="trade" tick={{ fontSize: 9 }} label={{ value: 'Trade #', position: 'insideBottomRight', offset: -4, fontSize: 9, fill: '#6b7280' }} />
                    <YAxis tick={{ fontSize: 9 }} tickFormatter={v => `${v}`} />
                    <Tooltip content={<ChartTooltip unit=" pips" />} />
                    <ReferenceLine y={0} stroke="#374151" strokeDasharray="3 3" />
                    <Area type="monotone" dataKey="cumulativePips"
                      stroke="#10b981" strokeWidth={2}
                      fill="url(#eqGrad)" dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-[180px] flex items-center justify-center text-xs text-gray-600">
                  Not enough completed trades to calculate reliable analytics.
                </div>
              )}
            </div>

            {/* Session performance */}
            <div className="bg-[#1e2535] rounded-xl p-4">
              <SectionHeader icon={Activity} label="Session Win Rate" color="text-amber-400" />
              {a.sessionBreakdown?.length > 0 ? (
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={a.sessionBreakdown} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#263044" />
                    <XAxis dataKey="session" tick={{ fontSize: 9 }} />
                    <YAxis tick={{ fontSize: 9 }} tickFormatter={v => `${v}%`} domain={[0, 100]} />
                    <Tooltip formatter={(v, n) => [
                      n === 'winRate' ? `${v}%` : `${v} pips`,
                      n === 'winRate' ? 'Win Rate' : 'Avg Pips',
                    ]} />
                    <Bar dataKey="winRate" radius={[3, 3, 0, 0]}>
                      {(a.sessionBreakdown ?? []).map((s, i) => (
                        <Cell key={i} fill={sessionColor(s)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-[180px] flex items-center justify-center text-xs text-gray-600">
                  No session data yet.
                </div>
              )}
              {/* Best / worst callout */}
              {(a.bestSession || a.worstSession) && (
                <div className="flex gap-4 mt-2">
                  {a.bestSession  && <div className="flex items-center gap-1.5"><div className="w-2 h-2 rounded-sm bg-emerald-500" /><span className="text-[10px] text-gray-500">Best: {a.bestSession}</span></div>}
                  {a.worstSession && <div className="flex items-center gap-1.5"><div className="w-2 h-2 rounded-sm bg-red-500" /><span className="text-[10px] text-gray-500">Worst: {a.worstSession}</span></div>}
                </div>
              )}
            </div>
          </div>

          {/* ── Directional + News row ───────────────────────────────── */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

            {/* Directional performance */}
            <div className="bg-[#1e2535] rounded-xl p-4">
              <SectionHeader icon={TrendingUp} label="Directional Performance" color="text-blue-400" />
              <DualStat
                labelA="BUY Win Rate" valA={pct(a.longWinRate)}
                clsA={a.longWinRate >= 50 ? 'text-emerald-400' : 'text-red-400'}
                labelB="SELL Win Rate" valB={pct(a.shortWinRate)}
                clsB={a.shortWinRate >= 50 ? 'text-emerald-400' : 'text-red-400'}
              />
              {a.longWinRate != null && a.shortWinRate != null && (
                <div className="mt-3 text-[10px] text-gray-600">
                  {a.longWinRate > a.shortWinRate
                    ? `Stronger edge on BUY setups (+${(a.longWinRate - a.shortWinRate).toFixed(1)}%)`
                    : a.shortWinRate > a.longWinRate
                    ? `Stronger edge on SELL setups (+${(a.shortWinRate - a.longWinRate).toFixed(1)}%)`
                    : 'Equal performance in both directions'}
                </div>
              )}
            </div>

            {/* News-day performance */}
            <div className="bg-[#1e2535] rounded-xl p-4">
              <SectionHeader icon={Activity} label="News-Day Performance" color="text-purple-400" />
              <DualStat
                labelA="News Day" valA={pct(a.newsDayWinRate)}
                clsA={a.newsDayWinRate != null ? (a.newsDayWinRate >= 50 ? 'text-emerald-400' : 'text-red-400') : 'text-gray-500'}
                labelB="Non-News Day" valB={pct(a.nonNewsDayWinRate)}
                clsB={a.nonNewsDayWinRate != null ? (a.nonNewsDayWinRate >= 50 ? 'text-emerald-400' : 'text-red-400') : 'text-gray-500'}
              />
              <div className="mt-3 text-[10px] text-gray-600">
                {a.newsDayWinRate == null
                  ? 'No confirmed trades on news days yet.'
                  : a.nonNewsDayWinRate > a.newsDayWinRate
                  ? 'Cleaner edge on non-news days — consider avoiding high-impact sessions.'
                  : 'News-day performance is comparable or stronger.'}
              </div>
            </div>
          </div>

          {/* ── Expectancy explainer ─────────────────────────────────── */}
          <div className="bg-[#1e2535] rounded-xl p-4">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Expectancy Formula</div>
                <div className="font-mono text-xs text-gray-300 mb-1">
                  ({pct(a.winRate)} × {a.averageWinPips != null ? Math.round(a.averageWinPips) : '—'} pips)
                  {' − '}
                  ({pct(a.lossRate)} × {a.averageLossPips != null ? Math.round(a.averageLossPips) : '—'} pips)
                  {' = '}
                  <span className={`font-bold ${a.expectancy > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {a.expectancy != null ? `${a.expectancy > 0 ? '+' : ''}${fmt(a.expectancy)} pips` : '—'}
                  </span>
                </div>
                <div className="text-[10px] text-gray-600">
                  Average profit expected per trade. Positive expectancy means the strategy is profitable over a large sample.
                  Win rate alone is misleading — a 40% win rate with 3:1 R:R outperforms a 70% win rate with 0.5:1.
                </div>
              </div>
              <div className="text-right flex-shrink-0">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Profit Factor</div>
                <div className="font-mono text-2xl font-black text-blue-400">
                  {fmt(a.profitFactor, 2) ?? '—'}
                </div>
                <div className="text-[9px] text-gray-600">winning pips / losing pips</div>
              </div>
            </div>
          </div>

        </div>
      )}
    </div>
  );
}
