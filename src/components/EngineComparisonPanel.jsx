/**
 * EngineComparisonPanel
 *
 * Runs the SAME historical window through every available engine variant
 * (swing, intraday, momentum_breakout) and shows side-by-side cards so
 * the operator can see which engine actually has edge — and which engines
 * are too strict to ever fire.
 *
 * One click → N backtests on identical bars → ranked comparison + verdict.
 */
import { useState } from 'react';
import {
  Crown, Play, RefreshCw, AlertTriangle, Layers, CheckCircle, XCircle,
  TrendingUp, TrendingDown, Clock, Activity,
} from 'lucide-react';
import { runEngineComparison } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';

const VARIANT_LABELS = {
  swing:             'Swing ICT',
  intraday:          'Intraday ICT',
  momentum_breakout: 'Momentum Breakout',
  trend_pullback:    'Trend Pullback',
  bb_reversion:      'BB Reversion',
  opening_range:     'Opening Range',
  asian_fade:        'Asian Fade',
};

function VariantCard({ row, isWinner }) {
  const s = row.summary;
  const hasTrades = s && s.validTrades > 0;
  const positive  = hasTrades && s.expectancyR > 0;
  const cls = isWinner
    ? 'border-emerald-500/60 bg-emerald-500/10'
    : hasTrades
      ? (positive ? 'border-blue-500/40 bg-blue-500/5' : 'border-amber-500/40 bg-amber-500/5')
      : 'border-slate-700 bg-slate-800/40';

  return (
    <div className={`rounded border-2 p-3 ${cls}`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          {isWinner && <Crown size={13} className="text-emerald-400" />}
          <span className="text-[11px] uppercase tracking-widest font-bold text-gray-100">
            {VARIANT_LABELS[row.name] || row.name}
          </span>
        </div>
        <span className="text-[9px] text-gray-500 font-mono">{row.timing_sec}s</span>
      </div>

      {row.error && (
        <div className="text-[10px] text-red-300 leading-tight">
          <XCircle size={10} className="inline mr-1" />
          {row.error}
        </div>
      )}

      {!row.error && s && (
        <>
          {/* Key metric — Expectancy R, big and bold */}
          {hasTrades ? (
            <div className="mb-2">
              <div className="text-[9px] uppercase tracking-widest text-gray-500">Expectancy R</div>
              <div className={`text-2xl font-mono font-bold ${
                positive ? 'text-emerald-400' : 'text-red-400'
              }`}>
                {s.expectancyR > 0 ? '+' : ''}{s.expectancyR.toFixed(2)}
              </div>
            </div>
          ) : (
            <div className="mb-2 py-2 rounded bg-amber-500/10 text-center">
              <AlertTriangle size={12} className="inline text-amber-400 mr-1" />
              <span className="text-[10px] uppercase tracking-widest font-bold text-amber-300">
                NO TRADES FIRED
              </span>
            </div>
          )}

          {/* Stats grid */}
          <div className="grid grid-cols-2 gap-1 text-[10px] font-mono">
            <Stat label="trades"   value={s.validTrades} />
            <Stat label="scanned"  value={s.candidatesScanned} />
            <Stat label="WR %"     value={s.winRate.toFixed(1)} cls={s.winRate >= 50 ? 'text-emerald-300' : 'text-gray-400'} />
            <Stat label="PF"       value={s.profitFactor != null ? s.profitFactor.toFixed(2) : '—'} />
            <Stat label="avg RR"   value={s.averageRR.toFixed(2)} />
            <Stat label="max DD %" value={s.maxDrawdownPercent.toFixed(1)} />
            <Stat label="BUY WR%"  value={s.buyWinRate.toFixed(1)} cls="text-emerald-300" />
            <Stat label="SELL WR%" value={s.sellWinRate.toFixed(1)} cls="text-red-300" />
          </div>

          {/* Sample trades */}
          {row.sample_trades?.length > 0 && (
            <div className="mt-2 pt-2 border-t border-current/20">
              <div className="text-[9px] uppercase tracking-widest text-gray-500 mb-1">
                First 3 trades
              </div>
              {row.sample_trades.map((t, i) => (
                <div key={i} className="text-[9px] font-mono leading-tight flex items-center gap-2">
                  {t.signal === 'BUY'
                    ? <TrendingUp size={9} className="text-emerald-400" />
                    : <TrendingDown size={9} className="text-red-400" />}
                  <span className="text-gray-500">{(t.time||'').slice(0,16)}</span>
                  <span className={t.result === 'WIN' ? 'text-emerald-300' : t.result === 'LOSS' ? 'text-red-300' : 'text-gray-400'}>
                    {t.result}
                  </span>
                  <span className="text-gray-400">{t.points > 0 ? '+' : ''}{t.points?.toFixed(0)}pts</span>
                </div>
              ))}
            </div>
          )}

          {/* Data source */}
          <div className="mt-2 text-[9px] text-gray-600">
            data: <span className={row.isSynthetic ? 'text-red-400' : 'text-emerald-400'}>
              {row.dataSource || 'unknown'}
            </span>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, cls = 'text-gray-200' }) {
  return (
    <div className="bg-black/20 rounded px-1.5 py-0.5">
      <div className="text-[8px] uppercase text-gray-500">{label}</div>
      <div className={cls}>{value}</div>
    </div>
  );
}

export default function EngineComparisonPanel() {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const [lookback, setLookback] = useState(5000);
  const [timeframe, setTimeframe] = useState('M15');
  const [minScore, setMinScore] = useState(65);
  const [minRr, setMinRr] = useState(1.5);

  async function run() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await runEngineComparison({
        timeframe, lookback, minScore, minRr,
      });
      setResult(r);
    } catch (e) {
      setError(e?.message ?? 'Comparison failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Activity size={16} className="text-purple-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Engine Comparison — which engine actually has edge?
          </h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            swing · intraday · momentum_breakout on same data
          </span>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <label className="text-gray-500">TF:</label>
          <select value={timeframe} onChange={e => setTimeframe(e.target.value)} disabled={loading}
            className="bg-[#161b27] border border-[#263044] rounded px-1.5 py-0.5 text-xs">
            {['M5','M15','M30','H1','H4'].map(t => <option key={t}>{t}</option>)}
          </select>
          <label className="text-gray-500 ml-1">Bars:</label>
          <select value={lookback} onChange={e => setLookback(Number(e.target.value))} disabled={loading}
            className="bg-[#161b27] border border-[#263044] rounded px-1.5 py-0.5 text-xs">
            {[1000, 2500, 5000, 10000].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
          <label className="text-gray-500 ml-1">Min score:</label>
          <select value={minScore} onChange={e => setMinScore(Number(e.target.value))} disabled={loading}
            className="bg-[#161b27] border border-[#263044] rounded px-1.5 py-0.5 text-xs">
            {[50, 60, 65, 70, 75, 80, 85].map(n => <option key={n}>{n}</option>)}
          </select>
          <label className="text-gray-500 ml-1">Min RR:</label>
          <select value={minRr} onChange={e => setMinRr(Number(e.target.value))} disabled={loading}
            className="bg-[#161b27] border border-[#263044] rounded px-1.5 py-0.5 text-xs">
            {[1.0, 1.5, 2.0, 2.5, 3.0].map(n => <option key={n}>{n}</option>)}
          </select>
          <button onClick={run} disabled={loading}
            className={`flex items-center gap-1 px-3 py-1 rounded text-xs font-bold ${
              loading
                ? 'bg-slate-700 text-slate-400'
                : 'bg-purple-500/20 border border-purple-500/60 text-purple-200 hover:bg-purple-500/30'
            }`}>
            {loading ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
            {loading ? 'Comparing…' : 'Compare Engines'}
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
          Run a side-by-side comparison of all 3 engines on the same historical
          window. Each engine gets identical bars, identical thresholds.
        </div>
      )}

      {loading && (
        <div className="text-center text-xs text-gray-400 py-8">
          <RefreshCw size={20} className="mx-auto mb-2 animate-spin text-purple-400" />
          Running N backtests sequentially… this can take 30-90 seconds.
        </div>
      )}

      {result && (
        <>
          {/* Verdict banner */}
          <div className={`rounded border-2 p-3 ${
            result.winner
              ? 'border-emerald-500/50 bg-emerald-500/10'
              : 'border-amber-500/40 bg-amber-500/10'
          }`}>
            <div className="flex items-start gap-2">
              {result.winner
                ? <Crown size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                : <AlertTriangle size={14} className="text-amber-400 shrink-0 mt-0.5" />}
              <div className={result.winner ? 'text-emerald-300' : 'text-amber-300'}>
                <div className="text-[10px] uppercase tracking-widest font-bold">
                  {result.winner ? `Winner: ${VARIANT_LABELS[result.winner] || result.winner}` : 'No clear winner'}
                </div>
                <div className="text-xs mt-1 opacity-90">{result.verdict}</div>
              </div>
            </div>
          </div>

          {/* Engine cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {result.variants.map(v => (
              <VariantCard key={v.name} row={v} isWinner={v.name === result.winner} />
            ))}
          </div>

          {/* Footer */}
          <div className="text-[10px] text-gray-600 text-right">
            <Clock size={10} className="inline mr-1" />
            Total: {result.total_sec}s · {formatKenyaTime(result.generated_at)} {KENYA_LABEL}
            {' · '}config: score≥{result.config.minScore} RR≥{result.config.minRR} risk={result.config.riskPercent}%
          </div>
        </>
      )}
    </div>
  );
}
