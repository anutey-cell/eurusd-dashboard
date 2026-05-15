/**
 * PaperObservationPanel
 *
 * The PAPER_OBSERVATION_ONLY workflow surface.
 *
 * The institutional scanner auto-logs every SIGNAL_READY state into the
 * paper_observations table (with fingerprint dedupe). This panel:
 *   - Shows the running tally
 *   - Displays progress toward the n>=30 certification threshold
 *   - Reports live WR / expectancy R / profit factor as samples accumulate
 *   - Lets the user trigger forward-resolution of pending observations
 *
 * Safety: pure analysis. Does not place trades or confirm signals.
 */
import { useEffect, useState, useCallback } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine, Line, ComposedChart,
} from 'recharts';
import {
  Eye, RefreshCw, CheckCircle, XCircle, Clock, Trophy,
  AlertTriangle, TrendingUp, TrendingDown, Minus, ChevronDown, ChevronUp,
  TrendingUp as DollarUp,
} from 'lucide-react';
import {
  getPaperObservations, getPaperObservationStats, resolvePaperObservations,
  compareEngines, runDualEngines, getEquityCurve, checkDrawdown,
} from '../services/api';

// ── Equity Curve Component ────────────────────────────────────────────────────

function EquityCurveCard({ engineId, color = '#3b82f6' }) {
  const [curve, setCurve] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getEquityCurve(engineId, 10000, 0.25);
      setCurve(data);
    } catch { /* ignore */ }
    setLoading(false);
  }, [engineId]);

  useEffect(() => { load(); }, [load]);

  if (!curve || curve.totalTrades === 0) {
    return (
      <div className="bg-[#0a0f17] border border-[#1e2535] rounded-lg p-3 text-center text-[10px] text-gray-600">
        No resolved observations for <span className="font-mono">{engineId}</span> yet
      </div>
    );
  }

  const positive = curve.finalEquity >= curve.initialEquity;
  const lineColor = positive ? '#10b981' : '#ef4444';

  return (
    <div className="bg-[#0a0f17] border border-[#1e2535] rounded-lg p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-widest font-semibold flex items-center gap-1.5"
              style={{ color }}>
          <DollarUp size={11} /> {engineId} Equity Curve
        </span>
        <span className={`text-[10px] font-mono ${positive ? 'text-emerald-400' : 'text-red-400'}`}>
          ${curve.finalEquity.toLocaleString(undefined, { maximumFractionDigits: 0 })}
          {' '}({curve.netReturnPct > 0 ? '+' : ''}{curve.netReturnPct}%)
        </span>
      </div>
      <ResponsiveContainer width="100%" height={140}>
        <ComposedChart data={curve.points} margin={{ top: 4, right: 4, bottom: 0, left: -20 }}>
          <defs>
            <linearGradient id={`eq-${engineId}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={lineColor} stopOpacity={0.35} />
              <stop offset="95%" stopColor={lineColor} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e2535" />
          <XAxis dataKey="observationId" tick={{ fill: '#6b7280', fontSize: 9 }} />
          <YAxis yAxisId="eq" tick={{ fill: '#6b7280', fontSize: 9 }} />
          <YAxis yAxisId="dd" orientation="right" tick={{ fill: '#9ca3af', fontSize: 9 }} unit="%" domain={[0, 'dataMax']} reversed />
          <Tooltip contentStyle={{ background: '#1a2035', border: '1px solid #263044', fontSize: 11 }} />
          <ReferenceLine yAxisId="eq" y={curve.initialEquity} stroke="#475569" strokeDasharray="4 2" />
          <Area yAxisId="eq" type="monotone" dataKey="equity"
                stroke={lineColor} fill={`url(#eq-${engineId})`} strokeWidth={1.5} dot={false} name="Equity" />
          <Line yAxisId="dd" type="monotone" dataKey="drawdownPct"
                stroke="#f59e0b" strokeWidth={1} dot={false} name="Drawdown %" />
        </ComposedChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-4 gap-1 text-[9px]">
        <div>
          <div className="text-gray-500 uppercase">Peak</div>
          <div className="font-mono">${curve.peakEquity.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
        </div>
        <div>
          <div className="text-gray-500 uppercase">Max DD</div>
          <div className={`font-mono ${curve.maxDrawdownPct > 10 ? 'text-red-400' : 'text-amber-400'}`}>
            {curve.maxDrawdownPct}%
          </div>
        </div>
        <div>
          <div className="text-gray-500 uppercase">Current DD</div>
          <div className="font-mono">{curve.currentDrawdownPct}%</div>
        </div>
        <div>
          <div className="text-gray-500 uppercase">Trades</div>
          <div className="font-mono">{curve.totalTrades}</div>
        </div>
      </div>
    </div>
  );
}

// ── Constants ─────────────────────────────────────────────────────────────────

const POLL_MS = 30_000;     // refresh stats every 30s

const READINESS_CFG = {
  AWAITING_FIRST_SIGNAL: {
    cls: 'border-gray-600/40 bg-gray-900/30 text-gray-400',
    label: 'AWAITING FIRST SIGNAL',
  },
  OBSERVATION_IN_PROGRESS: {
    cls: 'border-blue-600/40 bg-blue-900/30 text-blue-300',
    label: 'OBSERVATION IN PROGRESS',
  },
  EDGE_NOT_CONFIRMED: {
    cls: 'border-red-600/40 bg-red-900/30 text-red-300',
    label: 'EDGE NOT CONFIRMED',
  },
  EDGE_MARGINAL: {
    cls: 'border-amber-600/40 bg-amber-900/30 text-amber-300',
    label: 'EDGE MARGINAL',
  },
  EDGE_CONFIRMED_IN_OBSERVATION: {
    cls: 'border-emerald-600/40 bg-emerald-900/30 text-emerald-300',
    label: 'EDGE CONFIRMED',
  },
};

// ── Sub-components ────────────────────────────────────────────────────────────

function Stat({ label, value, color = 'text-white', sub }) {
  return (
    <div className="bg-[#161b27] border border-[#263044] rounded-lg p-2 flex flex-col gap-0.5">
      <span className="text-[9px] text-gray-500 uppercase tracking-widest">{label}</span>
      <span className={`font-mono font-bold text-sm ${color}`}>{value ?? '—'}</span>
      {sub && <span className="text-[9px] text-gray-600">{sub}</span>}
    </div>
  );
}

function ProgressBar({ pct, color = 'bg-blue-500' }) {
  const w = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <div className="space-y-1">
      <div className="h-1.5 bg-[#263044] rounded-full overflow-hidden">
        <div className={`h-full ${color} transition-all duration-500`} style={{ width: `${w}%` }} />
      </div>
    </div>
  );
}

function SignalChip({ signal }) {
  const cfg = {
    BUY:  { cls: 'bg-emerald-500/20 text-emerald-400', Icon: TrendingUp   },
    SELL: { cls: 'bg-red-500/20 text-red-400',         Icon: TrendingDown },
  }[signal] ?? { cls: 'bg-gray-500/20 text-gray-400', Icon: Minus };
  const { cls, Icon } = cfg;
  return (
    <span className={`inline-flex items-center gap-1 text-[9px] font-bold px-1.5 py-0.5 rounded font-mono ${cls}`}>
      <Icon size={9} /> {signal}
    </span>
  );
}

function ResultChip({ result }) {
  if (!result) return <span className="text-[9px] text-amber-400/70">pending</span>;
  const cfg = {
    WIN:     'bg-emerald-500/20 text-emerald-400',
    LOSS:    'bg-red-500/20 text-red-400',
    EXPIRED: 'bg-purple-500/20 text-purple-400',
  }[result] ?? 'bg-gray-500/20 text-gray-400';
  return <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${cfg}`}>{result}</span>;
}

// ── Main component ────────────────────────────────────────────────────────────

export default function PaperObservationPanel() {
  const [stats, setStats] = useState(null);
  const [comparison, setComparison] = useState(null);
  const [list,  setList]  = useState([]);
  const [filter, setFilter] = useState('all');
  const [engineFilter, setEngineFilter] = useState('all');
  const [loading, setLoading] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [running, setRunning] = useState(false);
  const [error,   setError]   = useState(null);
  const [showList, setShowList] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, cmp, lst] = await Promise.all([
        getPaperObservationStats(),
        compareEngines('swing', 'trend_pullback'),
        getPaperObservations({
          limit: 50, resolved: filter,
          ...(engineFilter !== 'all' ? { engine_id: engineFilter } : {}),
        }),
      ]);
      setStats(s);
      setComparison(cmp);
      setList(lst.observations ?? []);
    } catch (e) {
      setError(e.message ?? 'Failed to load observations');
    } finally {
      setLoading(false);
    }
  }, [filter, engineFilter]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  async function handleResolve() {
    setResolving(true);
    setError(null);
    try {
      await resolvePaperObservations();
      await refresh();
    } catch (e) {
      setError(e.message ?? 'Resolve failed');
    } finally {
      setResolving(false);
    }
  }

  async function handleRunDual() {
    setRunning(true);
    setError(null);
    try {
      await runDualEngines();
      await refresh();
    } catch (e) {
      setError(e.message ?? 'Dual-engine run failed');
    } finally {
      setRunning(false);
    }
  }

  const tier = READINESS_CFG[stats?.readiness] ?? READINESS_CFG.AWAITING_FIRST_SIGNAL;

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl overflow-hidden">

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#263044] bg-[#131c27]">
        <div className="flex items-center gap-2.5">
          <Eye size={15} className="text-blue-400" />
          <div>
            <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
              Paper Observation Tracker
            </h2>
            <p className="text-[10px] text-slate-500">
              PAPER_OBSERVATION_ONLY · Scanner SIGNAL_READY states auto-logged · No execution
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleRunDual}
            disabled={running}
            className="text-[10px] bg-purple-700 hover:bg-purple-600 text-white rounded px-2 py-1 disabled:opacity-50 flex items-center gap-1"
            title="Run BOTH swing + trend_pullback engines now, log qualifying signals"
          >
            {running
              ? <><RefreshCw size={10} className="animate-spin" /> Running…</>
              : <>Run dual engines</>
            }
          </button>
          <button
            onClick={handleResolve}
            disabled={resolving}
            className="text-[10px] bg-emerald-700 hover:bg-emerald-600 text-white rounded px-2 py-1 disabled:opacity-50 flex items-center gap-1"
            title="Walk pending observations forward through historical candles to determine WIN/LOSS"
          >
            {resolving
              ? <><RefreshCw size={10} className="animate-spin" /> Resolving…</>
              : <><CheckCircle size={10} /> Resolve outcomes</>
            }
          </button>
          <button onClick={refresh} disabled={loading} className="text-gray-500 hover:text-gray-300" title="Refresh">
            <RefreshCw size={12} className={loading ? 'animate-spin text-blue-400' : ''} />
          </button>
        </div>
      </div>

      <div className="p-5 space-y-4">

        {error && (
          <div className="flex items-center gap-2 text-xs text-red-400 bg-red-900/20 border border-red-800/40 rounded-lg px-3 py-2">
            <AlertTriangle size={13} />
            {error}
          </div>
        )}

        {/* Readiness banner */}
        <div className={`rounded-xl border p-4 space-y-2 ${tier.cls}`}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Trophy size={13} />
              <span className="text-[10px] uppercase tracking-widest font-bold">
                {tier.label}
              </span>
            </div>
            <div className="text-right">
              <div className="text-2xl font-black font-mono leading-none">
                {stats?.resolved ?? 0}<span className="text-sm opacity-50">/{stats?.certificationThreshold ?? 30}</span>
              </div>
              <div className="text-[9px] uppercase tracking-wider opacity-80">resolved observations</div>
            </div>
          </div>
          <ProgressBar
            pct={stats?.progressPercent ?? 0}
            color={
              stats?.readiness === 'EDGE_CONFIRMED_IN_OBSERVATION' ? 'bg-emerald-500' :
              stats?.readiness === 'EDGE_MARGINAL'                 ? 'bg-amber-500'   :
              stats?.readiness === 'EDGE_NOT_CONFIRMED'            ? 'bg-red-500'     :
                                                                     'bg-blue-500'
            }
          />
          <p className="text-[11px] opacity-90">{stats?.verdict ?? 'Loading…'}</p>
          {stats?.samplesNeeded > 0 && (
            <p className="text-[10px] opacity-70 italic">
              Need {stats.samplesNeeded} more confirmed outcome(s) to reach certification threshold.
            </p>
          )}
        </div>

        {/* Dual-engine comparison panel */}
        {comparison && (
          <div className="bg-[#131c27] border border-[#263044] rounded-xl p-3">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-medium text-slate-300 flex items-center gap-1.5">
                <Trophy size={11} className="text-amber-400" /> Dual-Engine Comparison
              </span>
              {comparison.leader && (
                <span className="text-[10px] text-emerald-400 font-mono">
                  Leader: {comparison.leader}  (Δ {comparison.leaderMargin?.toFixed(2)}R)
                </span>
              )}
            </div>
            <div className="grid grid-cols-2 gap-2">
              {['statsA', 'statsB'].map((key, idx) => {
                const s = comparison[key] ?? {};
                const eng = idx === 0 ? comparison.engineA : comparison.engineB;
                const isLeader = comparison.leader === eng;
                return (
                  <div key={eng} className={`rounded-lg p-2 space-y-1 border ${
                    isLeader ? 'border-emerald-700/40 bg-emerald-900/15'
                              : 'border-[#1e2535] bg-[#0a0f17]'
                  }`}>
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-bold uppercase tracking-wider text-slate-300">
                        {eng.replace('_', ' ')}
                      </span>
                      {isLeader && (
                        <span className="text-[9px] text-emerald-400">★ leading</span>
                      )}
                    </div>
                    <div className="grid grid-cols-3 gap-1">
                      <div>
                        <div className="text-[9px] text-gray-500 uppercase">Resolved</div>
                        <div className="font-mono text-xs">{s.resolved ?? 0}</div>
                      </div>
                      <div>
                        <div className="text-[9px] text-gray-500 uppercase">WR</div>
                        <div className={`font-mono text-xs ${(s.winRate ?? 0) >= 40 ? 'text-emerald-400' : 'text-amber-400'}`}>
                          {s.winRate ?? 0}%
                        </div>
                      </div>
                      <div>
                        <div className="text-[9px] text-gray-500 uppercase">Exp R</div>
                        <div className={`font-mono text-xs ${(s.expectancyR ?? 0) > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {(s.expectancyR ?? 0) > 0 ? '+' : ''}{s.expectancyR ?? 0}R
                        </div>
                      </div>
                      <div>
                        <div className="text-[9px] text-gray-500 uppercase">PF</div>
                        <div className="font-mono text-xs">
                          {s.profitFactor != null ? s.profitFactor.toFixed(2) : 'N/A'}
                        </div>
                      </div>
                      <div>
                        <div className="text-[9px] text-gray-500 uppercase">Pending</div>
                        <div className="font-mono text-xs text-amber-400">{s.pending ?? 0}</div>
                      </div>
                      <div>
                        <div className="text-[9px] text-gray-500 uppercase">Progress</div>
                        <div className="font-mono text-xs">{s.progressPercent ?? 0}%</div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Equity curves — side by side */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <EquityCurveCard engineId="swing"          color="#3b82f6" />
          <EquityCurveCard engineId="trend_pullback" color="#a78bfa" />
        </div>

        {/* Stats grid */}
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
          <Stat label="Logged"   value={stats?.total ?? 0} />
          <Stat label="Resolved" value={stats?.resolved ?? 0}
                color={stats?.resolved >= 30 ? 'text-emerald-400' : 'text-gray-300'} />
          <Stat label="Pending"  value={stats?.pending ?? 0}
                color={stats?.pending > 0 ? 'text-amber-400' : 'text-gray-300'} />
          <Stat label="Wins"     value={stats?.wins ?? 0}     color="text-emerald-400" />
          <Stat label="Losses"   value={stats?.losses ?? 0}   color="text-red-400" />
          <Stat label="Win rate" value={`${stats?.winRate ?? 0}%`}
                color={(stats?.winRate ?? 0) >= 40 ? 'text-emerald-400' : 'text-amber-400'} />
          <Stat label="Exp R"    value={`${(stats?.expectancyR ?? 0) > 0 ? '+' : ''}${stats?.expectancyR ?? 0}R`}
                color={(stats?.expectancyR ?? 0) > 0 ? 'text-emerald-400' : 'text-red-400'} />
          <Stat label="PF"       value={stats?.profitFactor != null ? stats.profitFactor.toFixed(2) : 'N/A'}
                color={(stats?.profitFactor ?? 0) >= 1.2 ? 'text-emerald-400' : 'text-red-400'} />
        </div>

        {/* Observation list (collapsed by default) */}
        {(stats?.total ?? 0) > 0 && (
          <div className="border-t border-[#263044] pt-3">
            <button
              onClick={() => setShowList(v => !v)}
              className="w-full flex items-center justify-between text-xs text-gray-400 hover:text-gray-200 py-1"
            >
              <span className="flex items-center gap-2">
                {showList ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                Observation List ({list.length})
              </span>
              <div className="flex items-center gap-1">
                {['all', 'resolved', 'pending'].map(f => (
                  <button
                    key={f}
                    onClick={(e) => { e.stopPropagation(); setFilter(f); }}
                    className={`text-[9px] px-1.5 py-0.5 rounded ${
                      filter === f ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {f}
                  </button>
                ))}
                <span className="text-gray-700 mx-1">|</span>
                {['all', 'swing', 'trend_pullback'].map(e => (
                  <button
                    key={e}
                    onClick={(ev) => { ev.stopPropagation(); setEngineFilter(e); }}
                    className={`text-[9px] px-1.5 py-0.5 rounded ${
                      engineFilter === e ? 'bg-purple-600 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {e === 'all' ? 'both' : e.replace('_', ' ')}
                  </button>
                ))}
              </div>
            </button>

            {showList && (
              <div className="mt-2 overflow-x-auto rounded-lg border border-[#263044] max-h-96 overflow-y-auto">
                <table className="w-full text-[10px]">
                  <thead className="sticky top-0 bg-[#131c27]">
                    <tr className="text-gray-500 uppercase tracking-wider">
                      {['#', 'Engine', 'Observed', 'Side', 'Entry', 'SL', 'TP', 'RR', 'Score', 'Setup', 'Session', 'Result', 'R', 'Pts'].map(h => (
                        <th key={h} className="text-left py-1 px-1.5 border-b border-[#263044] whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {list.map(o => (
                      <tr key={o.id} className="border-b border-[#1a2035] hover:bg-[#161b27]">
                        <td className="py-1 px-1.5 font-mono text-gray-600">{o.id}</td>
                        <td className={`py-1 px-1.5 text-[9px] font-bold uppercase ${
                          o.engineId === 'swing' ? 'text-blue-400' :
                          o.engineId === 'trend_pullback' ? 'text-purple-400' : 'text-gray-500'
                        }`}>{o.engineId || 'swing'}</td>
                        <td className="py-1 px-1.5 font-mono text-gray-400 whitespace-nowrap">
                          {o.observedAt?.slice(0, 16).replace('T', ' ')}
                        </td>
                        <td className="py-1 px-1.5"><SignalChip signal={o.signal} /></td>
                        <td className="py-1 px-1.5 font-mono text-blue-300">{o.entry?.toFixed(2)}</td>
                        <td className="py-1 px-1.5 font-mono text-red-400/70">{o.stopLoss?.toFixed(2)}</td>
                        <td className="py-1 px-1.5 font-mono text-emerald-400/70">{o.takeProfit?.toFixed(2)}</td>
                        <td className="py-1 px-1.5 font-mono text-blue-300">1:{o.rr?.toFixed(2) ?? '—'}</td>
                        <td className="py-1 px-1.5 font-mono text-gray-500">{o.score}</td>
                        <td className="py-1 px-1.5 text-gray-400 max-w-[140px] truncate" title={o.setupType}>
                          {o.setupType?.replace(/_/g, ' ')}
                        </td>
                        <td className="py-1 px-1.5 text-gray-400">{o.session}</td>
                        <td className="py-1 px-1.5"><ResultChip result={o.result} /></td>
                        <td className={`py-1 px-1.5 font-mono ${(o.rMultiple ?? 0) > 0 ? 'text-emerald-400' : (o.rMultiple ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                          {o.rMultiple != null ? `${o.rMultiple > 0 ? '+' : ''}${o.rMultiple.toFixed(2)}R` : '—'}
                        </td>
                        <td className={`py-1 px-1.5 font-mono ${(o.pointsCaptured ?? 0) > 0 ? 'text-emerald-400' : (o.pointsCaptured ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                          {o.pointsCaptured != null ? `${o.pointsCaptured > 0 ? '+' : ''}${o.pointsCaptured.toFixed(1)}` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {list.length === 0 && (
                  <p className="text-center text-[10px] text-gray-600 py-3 italic">
                    No observations match filter "{filter}".
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {(stats?.total ?? 0) === 0 && (
          <div className="border border-dashed border-[#263044] rounded-lg p-4 text-center text-[11px] text-gray-500">
            No observations logged yet. The institutional scanner will auto-log
            every <span className="font-mono">SIGNAL_READY</span> state.
            Click <span className="font-mono">Scan Institutional Setup</span> to generate one if conditions align.
          </div>
        )}

        {/* Safety footer */}
        <div className="flex items-center gap-2 text-[9px] text-slate-600 bg-slate-900/40 rounded-lg px-3 py-2 border border-slate-800">
          <Clock size={9} />
          Observations are <strong>passive records</strong>. No trades are placed. Outcomes are
          determined by walking H4 candles forward against TP/SL — same logic as the backtester.
        </div>
      </div>
    </section>
  );
}
