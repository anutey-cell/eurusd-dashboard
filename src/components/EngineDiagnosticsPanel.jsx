/**
 * EngineDiagnosticsPanel
 *
 * Answers "why no signal right now?" with:
 *   - Live price + 24h range (so you can sanity-check what you're seeing)
 *   - Biggest M15 move in last 24h (was THERE a move to catch?)
 *   - Data freshness per timeframe (M5/M15/H1/H4)
 *   - Per-engine status with the exact blocking reason
 *
 * Polls every 30s. Read-only.
 */
import { useState, useCallback } from 'react';
import {
  Activity, AlertTriangle, CheckCircle, XCircle, RefreshCw,
  TrendingUp, TrendingDown, Clock,
} from 'lucide-react';
import { getEngineState } from '../services/api';
import { formatKenyaTime, formatKenyaDateTime, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 30_000;

function StatPill({ ok, label, value }) {
  return (
    <div className={`rounded border p-2 ${
      ok ? 'border-emerald-500/40 bg-emerald-500/10' : 'border-red-500/40 bg-red-500/10'
    }`}>
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-widest text-gray-400">{label}</span>
        {ok ? <CheckCircle size={11} className="text-emerald-400" />
            : <XCircle    size={11} className="text-red-400" />}
      </div>
      <div className={`text-xs font-mono mt-0.5 ${ok ? 'text-emerald-300' : 'text-red-300'}`}>
        {value}
      </div>
    </div>
  );
}

function FreshnessRow({ f }) {
  if (f.error) {
    return (
      <tr className="border-b border-[#1c2333]">
        <td className="py-1 text-xs font-mono text-gray-300">{f.timeframe}</td>
        <td colSpan={4} className="py-1 text-xs text-red-300">{f.error}</td>
      </tr>
    );
  }
  return (
    <tr className="border-b border-[#1c2333]">
      <td className="py-1 text-xs font-mono text-gray-300">{f.timeframe}</td>
      <td className="py-1 text-xs font-mono text-gray-500">{f.source}</td>
      <td className="py-1 text-xs font-mono text-right">
        <span className={f.isFresh ? 'text-emerald-300' : 'text-red-300'}>
          {f.ageMinutes?.toFixed(0)} min
        </span>
        <span className="text-gray-600"> / {f.staleThreshold} max</span>
      </td>
      <td className="py-1 text-center">
        {f.isLive
          ? <span className="text-[9px] font-bold text-emerald-300">LIVE</span>
          : <span className="text-[9px] font-bold text-red-300">{(f.source||'?').toUpperCase()}</span>}
      </td>
      <td className="py-1 text-center">
        {f.isFresh
          ? <CheckCircle size={11} className="inline text-emerald-400" />
          : <XCircle     size={11} className="inline text-red-400" />}
      </td>
    </tr>
  );
}

function EngineCard({ engine, info }) {
  const label = {
    swing: 'Swing ICT',
    trend_pullback: 'Trend Pullback',
    momentum_breakout: 'Momentum Breakout',
  }[engine] || engine;

  const ready = info?.passing;
  const cls = ready
    ? 'border-emerald-500/40 bg-emerald-500/10'
    : 'border-amber-500/40 bg-amber-500/10';

  return (
    <div className={`rounded border p-3 ${cls}`}>
      <div className="flex items-center justify-between mb-1">
        <span className="text-[11px] uppercase tracking-widest font-bold text-gray-200">
          {label}
        </span>
        {ready
          ? <span className="text-[9px] font-bold text-emerald-300">READY</span>
          : <span className="text-[9px] font-bold text-amber-300">{info?.status || 'WAIT'}</span>}
      </div>
      <div className="text-[10px] text-gray-400 font-mono leading-tight">
        signal: {info?.signal ?? '—'} · score: {info?.score ?? info?.qualityScore ?? '—'}
      </div>
      <div className="text-[10px] text-gray-300 mt-2 leading-snug">
        {info?.reason || info?.summary || (info?.blockers?.[0]) || '—'}
      </div>
      {info?.error && (
        <div className="text-[10px] text-red-300 mt-1 font-mono">err: {info.error}</div>
      )}
    </div>
  );
}

export default function EngineDiagnosticsPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await getEngineState();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Diagnostics fetch failed');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(load, POLL_MS);

  const snap = data?.priceSnapshot || {};
  const biggest = snap.biggestM15Move;

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Activity size={16} className="text-amber-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Engine Diagnostics — Why no signal?
          </h2>
          {data?.verdict && (
            <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-0.5 rounded border ${
              data.anyEngineReady
                ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300'
                : 'border-amber-500/50 bg-amber-500/10 text-amber-300'
            }`}>
              {data.anyEngineReady ? 'AT LEAST ONE READY' : 'ALL WAITING'}
            </span>
          )}
        </div>
        <button onClick={load} disabled={loading}
          className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-300">
          <RefreshCw size={10} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded p-2 text-xs text-red-300 flex items-center gap-2">
          <AlertTriangle size={12} />
          {error}
        </div>
      )}

      {data && (
        <>
          {/* Price snapshot */}
          {snap.currentPrice != null && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              <StatPill ok={true}
                label="Current"
                value={`$${snap.currentPrice.toFixed(2)}`} />
              <StatPill ok={snap.netChangePct >= 0}
                label="24h Change"
                value={`${snap.netChangePct >= 0 ? '+' : ''}${snap.netChangePct}% ($${snap.netChange.toFixed(2)})`} />
              <StatPill ok={true}
                label="24h High"
                value={`$${snap.high24h.toFixed(2)}`} />
              <StatPill ok={true}
                label="24h Range"
                value={`$${snap.range24h.toFixed(2)}`} />
            </div>
          )}

          {/* Biggest move callout — was there something to catch? */}
          {biggest && (
            <div className="rounded border border-blue-500/40 bg-blue-500/5 p-3 flex items-center justify-between flex-wrap gap-2">
              <div className="flex items-center gap-2">
                {biggest.direction === 'BUY'
                  ? <TrendingUp size={14} className="text-emerald-400" />
                  : <TrendingDown size={14} className="text-red-400" />}
                <span className="text-xs font-bold text-gray-200">
                  Biggest M15 move last 24h:
                </span>
                <span className={`text-xs font-mono font-bold ${biggest.direction === 'BUY' ? 'text-emerald-300' : 'text-red-300'}`}>
                  {biggest.direction} ${biggest.body.toFixed(2)} body (${biggest.range.toFixed(2)} range)
                </span>
                <span className="text-[10px] text-gray-500">
                  vol {biggest.volume.toLocaleString()}
                </span>
              </div>
              <span className="text-[10px] text-gray-500 font-mono">
                {biggest.time && formatKenyaDateTime(biggest.time)} {KENYA_LABEL}
              </span>
            </div>
          )}

          {/* Data freshness */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-2">
              Data Freshness · {data.dataAllFresh ? 'all green ✓' : 'has stale TF ⚠'}
            </div>
            <table className="w-full">
              <thead>
                <tr className="text-[10px] uppercase tracking-widest text-gray-500 border-b border-[#263044]">
                  <th className="text-left py-1">TF</th>
                  <th className="text-left">Source</th>
                  <th className="text-right">Age</th>
                  <th className="text-center">Live?</th>
                  <th className="text-center">OK</th>
                </tr>
              </thead>
              <tbody>
                {data.dataFreshness.map(f => <FreshnessRow key={f.timeframe} f={f} />)}
              </tbody>
            </table>
          </div>

          {/* Learning-data status (historical_candles backing paper observations + backtests) */}
          {data.learningData && (
            <div className={`rounded border-2 p-3 ${
              data.learningData.needsBackfill
                ? 'border-red-500/50 bg-red-500/10'
                : 'border-emerald-500/40 bg-emerald-500/5'
            }`}>
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                  {data.learningData.needsBackfill
                    ? <AlertTriangle size={12} className="text-red-400" />
                    : <CheckCircle   size={12} className="text-emerald-400" />}
                  <span className={`text-[11px] uppercase tracking-widest font-bold ${
                    data.learningData.needsBackfill ? 'text-red-300' : 'text-emerald-300'
                  }`}>
                    Learning data — {data.learningData.needsBackfill ? 'NEEDS BACKFILL' : 'real history ready'}
                  </span>
                </div>
                <div className="text-[10px] font-mono text-gray-400">
                  real: <span className="text-emerald-300">{data.learningData.realCount}</span>
                  {' · '}
                  synthetic: <span className={data.learningData.syntheticCount > 0 ? 'text-red-300' : 'text-gray-500'}>{data.learningData.syntheticCount}</span>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2 mt-2">
                {Object.entries(data.learningData.details || {}).map(([tf, c]) => (
                  <div key={tf} className="text-[10px] font-mono bg-black/20 rounded px-2 py-1">
                    <div className="text-gray-400 uppercase tracking-widest text-[9px]">{tf}</div>
                    <div className={c.real > 0 ? 'text-emerald-300' : 'text-gray-500'}>real {c.real}</div>
                    <div className={c.synthetic > 0 ? 'text-red-300' : 'text-gray-600'}>synth {c.synthetic}</div>
                  </div>
                ))}
              </div>
              {data.learningData.needsBackfill && (
                <div className="text-[10px] text-red-200 mt-2 leading-tight">
                  Paper observations resolve against this dataset. Adaptive weights learn from those observations. Until real history is backfilled, the learning engine is operating on either no data or synthetic data — its conclusions are not trustworthy. Use the <b>Purge & Backfill</b> button in the Probability Sweep panel below, or POST /api/v1/backtest/fetch-tradingview directly.
                </div>
              )}
            </div>
          )}

          {/* Per-engine status */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-2">
              Engines (3) — gate states
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              <EngineCard engine="swing"              info={data.engines.swing} />
              <EngineCard engine="trend_pullback"     info={data.engines.trend_pullback} />
              <EngineCard engine="momentum_breakout"  info={data.engines.momentum_breakout} />
            </div>
          </div>

          <div className="text-[10px] text-gray-600 text-right flex items-center justify-end gap-2">
            <Clock size={10} />
            Updated {formatKenyaTime(data.generatedAt)} {KENYA_LABEL}
          </div>
        </>
      )}
    </div>
  );
}
