/**
 * ProbabilitySweepPanel
 *
 * Runs a backtest probability sweep — ONE backtest at the loosest gates
 * (min_score=lowest, min_rr=lowest), then post-filters the trade list to
 * evaluate every (min_score × min_rr) combination in milliseconds.
 *
 * Shows a ranked table of edge metrics per combo so the operator can spot
 * which threshold pair produces the best risk-adjusted edge.
 *
 * 12-combo sweep takes ~1 backtest worth of time (~25-40s vs the naive
 * 5-12 minutes that running 12 separate backtests would take).
 */
import { useState } from 'react';
import {
  Target, Play, RefreshCw, AlertTriangle, Crown, TrendingUp, TrendingDown,
  Zap, Clock, Layers,
} from 'lucide-react';
import { runProbabilitySweep, purgeSyntheticHistory, backfillTradingViewHistory } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';

const DEFAULT_SCORES = [65, 70, 75, 80, 85, 90];
const DEFAULT_RRS    = [1.5, 2.0, 2.5, 3.0];

const VERDICT_CFG = {
  'high-edge':     { cls: 'bg-emerald-500/20 border-emerald-500/60 text-emerald-300', label: 'HIGH EDGE' },
  'positive-edge': { cls: 'bg-blue-500/20    border-blue-500/60    text-blue-300',    label: 'POSITIVE' },
  'marginal':      { cls: 'bg-amber-500/20   border-amber-500/60   text-amber-300',   label: 'MARGINAL' },
  'negative':      { cls: 'bg-red-500/20     border-red-500/60     text-red-300',     label: 'NEGATIVE' },
  'low-sample':    { cls: 'bg-slate-700/40   border-slate-600/60   text-slate-400',   label: 'LOW N' },
  'no-trades':     { cls: 'bg-slate-700/40   border-slate-600/60   text-slate-500',   label: 'NO TRADES' },
};

function ResultTable({ rows, best }) {
  if (!rows?.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-gray-400 uppercase tracking-widest text-[10px] border-b border-[#263044]">
            <th className="text-center py-2">Score ≥</th>
            <th className="text-center">RR ≥</th>
            <th className="text-right">Trades</th>
            <th className="text-right">Win %</th>
            <th className="text-right">Exp R</th>
            <th className="text-right">Exp pts</th>
            <th className="text-right">PF</th>
            <th className="text-right">Avg RR</th>
            <th className="text-center">BUY · SELL</th>
            <th className="text-right">Return %</th>
            <th className="text-center">Verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const isBest = best && r.minScore === best.minScore && r.minRR === best.minRR;
            const cfg = VERDICT_CFG[r.verdict] ?? VERDICT_CFG['marginal'];
            return (
              <tr
                key={`${r.minScore}-${r.minRR}`}
                className={`border-b border-[#1c2333] hover:bg-[#1c2333]/40 ${isBest ? 'bg-emerald-500/5' : ''}`}
              >
                <td className="text-center py-2 font-mono">
                  <div className="flex items-center justify-center gap-1">
                    {isBest && <Crown size={11} className="text-emerald-400" title="Best edge" />}
                    <span className="font-bold text-white">{r.minScore}</span>
                  </div>
                </td>
                <td className="text-center font-mono text-gray-300">{r.minRR.toFixed(1)}</td>
                <td className="text-right font-mono text-gray-400">{r.validTrades}</td>
                <td className={`text-right font-mono ${r.winRate >= 50 ? 'text-emerald-400' : 'text-gray-400'}`}>
                  {r.winRate.toFixed(1)}
                </td>
                <td className={`text-right font-mono font-bold ${r.expectancyR > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {r.expectancyR > 0 ? '+' : ''}{r.expectancyR.toFixed(2)}
                </td>
                <td className={`text-right font-mono ${r.expectancyPoints > 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                  {r.expectancyPoints > 0 ? '+' : ''}{r.expectancyPoints.toFixed(2)}
                </td>
                <td className="text-right font-mono text-gray-400">
                  {r.profitFactor != null ? r.profitFactor.toFixed(2) : '—'}
                </td>
                <td className="text-right font-mono text-gray-400">{r.averageRR.toFixed(2)}</td>
                <td className="text-center font-mono text-[10px]">
                  <span className="text-emerald-400">{r.buyCount}</span>
                  <span className="text-gray-600 mx-0.5">·</span>
                  <span className="text-red-400">{r.sellCount}</span>
                </td>
                <td className={`text-right font-mono ${r.totalReturnPercent > 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                  {r.totalReturnPercent > 0 ? '+' : ''}{r.totalReturnPercent.toFixed(2)}
                </td>
                <td className="text-center">
                  <span className={`inline-block px-1.5 py-0.5 rounded text-[9px] font-bold border ${cfg.cls}`}>
                    {cfg.label}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function BestCalloutCard({ best, timing, base, grid }) {
  if (!best) return null;
  return (
    <div className="rounded-lg border-2 border-emerald-500/50 bg-emerald-500/5 p-4 space-y-2">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Crown size={16} className="text-emerald-400" />
          <span className="text-xs uppercase tracking-widest text-emerald-300 font-bold">
            Best Threshold Combo
          </span>
        </div>
        <div className="text-[10px] text-gray-400 font-mono">
          <Zap size={10} className="inline mr-1 text-amber-400" />
          {timing.total_sec}s for {grid.total_combinations} combos
          {' '}<span className="text-amber-300">({timing.speedup}x speedup)</span>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="Score ≥"      value={best.minScore} bold />
        <Stat label="RR ≥"         value={best.minRR.toFixed(1)} bold />
        <Stat label="Win Rate"     value={`${best.winRate.toFixed(1)}%`} valueCls="text-emerald-300" />
        <Stat label="Expectancy"   value={`${best.expectancyR > 0 ? '+' : ''}${best.expectancyR.toFixed(2)}R`} valueCls="text-emerald-300" />
        <Stat label="Trades"       value={best.validTrades} />
      </div>

      <div className="text-[10px] text-gray-400 pt-1 border-t border-emerald-500/20">
        Base run scanned {base.candidatesScanned} candidates → {base.validTrades} qualified
        at loosest gates (score ≥ {base.minScoreUsed}, RR ≥ {base.minRRUsed}).
        Filtered for {grid.total_combinations} threshold combinations.
      </div>
    </div>
  );
}

function Stat({ label, value, valueCls = 'text-white', bold = false }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-gray-500">{label}</div>
      <div className={`font-mono ${bold ? 'text-lg font-bold' : 'text-base'} ${valueCls}`}>
        {value}
      </div>
    </div>
  );
}

export default function ProbabilitySweepPanel() {
  const [result, setResult]     = useState(null);
  const [loading, setLoading]   = useState(false);
  const [error, setError]       = useState(null);

  // Tunable inputs
  const [lookback, setLookback]   = useState(5000);
  const [timeframe, setTimeframe] = useState('M15');
  const [engine,    setEngine]    = useState('swing');

  async function run() {
    setLoading(true);
    setError(null);
    try {
      const r = await runProbabilitySweep({
        timeframe, lookback,
        engineVariant: engine,
        minScores: DEFAULT_SCORES,
        minRrs:    DEFAULT_RRS,
      });
      setResult(r);
    } catch (e) {
      setError(e?.message ?? 'Sweep failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Target size={16} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Backtest Probability Sweep
          </h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            One backtest → all {DEFAULT_SCORES.length}×{DEFAULT_RRS.length} threshold combinations evaluated in ms
          </span>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <label className="text-gray-500">TF:</label>
          <select
            className="bg-[#161b27] border border-[#263044] rounded px-2 py-0.5 text-gray-200 text-xs"
            value={timeframe}
            onChange={e => setTimeframe(e.target.value)}
            disabled={loading}
          >
            {['M5','M15','M30','H1','H4'].map(tf => <option key={tf} value={tf}>{tf}</option>)}
          </select>
          <label className="text-gray-500 ml-1">Bars:</label>
          <select
            className="bg-[#161b27] border border-[#263044] rounded px-2 py-0.5 text-gray-200 text-xs"
            value={lookback}
            onChange={e => setLookback(Number(e.target.value))}
            disabled={loading}
          >
            {[1000, 2500, 5000, 10000, 20000].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
          <label className="text-gray-500 ml-1">Engine:</label>
          <select
            className="bg-[#161b27] border border-[#263044] rounded px-2 py-0.5 text-gray-200 text-xs"
            value={engine}
            onChange={e => setEngine(e.target.value)}
            disabled={loading}
          >
            <option value="swing">swing</option>
            <option value="intraday">intraday</option>
          </select>
          <button
            onClick={run}
            disabled={loading}
            className={`flex items-center gap-1 px-3 py-1 rounded text-xs font-bold ${
              loading
                ? 'bg-slate-700 text-slate-400'
                : 'bg-blue-500/20 border border-blue-500/60 text-blue-200 hover:bg-blue-500/30'
            }`}
          >
            {loading ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
            {loading ? 'Sweeping…' : 'Run Sweep'}
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded p-3 text-xs text-red-300 flex items-center gap-2">
          <AlertTriangle size={12} />
          {error}
        </div>
      )}

      {!result && !loading && !error && (
        <div className="text-center text-xs text-gray-500 py-8 border border-dashed border-slate-700 rounded">
          <Layers size={20} className="mx-auto mb-2 text-slate-600" />
          Click <span className="text-blue-300 font-semibold">Run Sweep</span> to evaluate{' '}
          {DEFAULT_SCORES.length}×{DEFAULT_RRS.length}={DEFAULT_SCORES.length * DEFAULT_RRS.length}{' '}
          score-RR combinations at once. Single backtest cost.
        </div>
      )}

      {loading && (
        <div className="text-center text-xs text-gray-400 py-8">
          <RefreshCw size={20} className="mx-auto mb-2 animate-spin text-blue-400" />
          Running one backtest at the loosest gates… post-filtering will follow instantly.
        </div>
      )}

      {result && (
        <>
          {result.isSynthetic && (
            <div className="rounded border-2 border-red-500/50 bg-red-500/10 p-3 text-xs text-red-300 space-y-2">
              <div className="flex items-start gap-2">
                <AlertTriangle size={14} className="shrink-0 mt-0.5" />
                <div>
                  <div className="font-bold uppercase tracking-widest text-[11px]">
                    Results based on {result.dataSource || 'synthetic'} data
                  </div>
                  <div className="text-[10px] opacity-90 mt-1">
                    {result.dataWarning ||
                      'Backtest ran on synthetic/seeded data — probabilities are NOT a real-market edge measurement.'}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2 pl-6 flex-wrap">
                <button
                  onClick={async () => {
                    if (!confirm(`Purge all synthetic ${timeframe} rows and refetch real ${timeframe} history from TradingView? This deletes any seeded data.`)) return;
                    setLoading(true);
                    try {
                      const p = await purgeSyntheticHistory({ timeframe });
                      const f = await backfillTradingViewHistory({ timeframe, nBars: lookback });
                      alert(`Purged ${p.deleted} synthetic rows. Fetched ${f.candlesInserted || 0} real ${timeframe} bars from TradingView.`);
                      await run();
                    } catch (e) {
                      alert('Recovery failed: ' + (e?.message || 'unknown'));
                    } finally {
                      setLoading(false);
                    }
                  }}
                  disabled={loading}
                  className="px-2 py-1 rounded bg-red-500/20 border border-red-500/60 text-red-200 hover:bg-red-500/30 text-[10px] font-bold uppercase tracking-widest"
                >
                  Purge & Backfill from TradingView
                </button>
                <span className="text-[10px] opacity-60">(deletes synthetic, pulls real history, re-runs sweep)</span>
              </div>
            </div>
          )}
          <BestCalloutCard
            best={result.best_combo}
            timing={result.timing}
            base={result.base_run}
            grid={result.grid}
          />
          <ResultTable rows={result.rows} best={result.best_combo} />
          <div className="text-[10px] text-gray-600 text-right">
            <Clock size={10} className="inline mr-1" />
            {result.timing.total_sec}s
            {result.timing.naive_estimate_sec > result.timing.total_sec &&
              <> · saved ~{Math.round(result.timing.naive_estimate_sec - result.timing.total_sec)}s vs running each backtest separately</>
            }
            {' · '}{formatKenyaTime(result.generated_at)} {KENYA_LABEL}
          </div>
        </>
      )}
    </div>
  );
}
