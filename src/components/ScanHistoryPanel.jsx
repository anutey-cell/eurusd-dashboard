/**
 * ScanHistoryPanel
 *
 * Displays the last N institutional scan records from the database.
 * Refreshes automatically whenever a new forced scan completes.
 *
 * Shows: timestamp, market state, signal, bias, confidence, readiness, summary snippet.
 * Does NOT show execution controls — analysis only.
 */
import { useState, useEffect, useCallback } from 'react';
import {
  History, RefreshCw, TrendingUp, TrendingDown, Minus,
  AlertTriangle, Clock,
} from 'lucide-react';
import { getScanHistory } from '../services/api';

// ── State colour map ──────────────────────────────────────────────────────────

const STATE_CLS = {
  SIGNAL_READY:        'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  SETUP_FORMING:       'bg-blue-500/15    text-blue-400    border-blue-500/30',
  WATCHLIST:           'bg-amber-500/15   text-amber-400   border-amber-500/30',
  NO_TRADE:            'bg-gray-700/30    text-gray-500    border-gray-700/30',
  NEWS_BLOCKED:        'bg-red-500/15     text-red-400     border-red-500/30',
  DATA_STALE:          'bg-orange-500/15  text-orange-400  border-orange-500/30',
  SPREAD_TOO_WIDE:     'bg-red-500/15     text-red-400     border-red-500/30',
  VOLATILITY_UNSTABLE: 'bg-purple-500/15  text-purple-400  border-purple-500/30',
};

const SIGNAL_ICON = {
  BUY:  { Icon: TrendingUp,   cls: 'text-emerald-400' },
  SELL: { Icon: TrendingDown, cls: 'text-red-400'     },
  WAIT: { Icon: Minus,        cls: 'text-gray-500'    },
};

const BIAS_CLS = {
  Bullish:           'text-emerald-400',
  Bearish:           'text-red-400',
  Neutral:           'text-gray-500',
  'Range-bound':     'text-amber-400',
  Compression:       'text-amber-400',
  Expansion:         'text-blue-400',
  'Reversal forming':'text-purple-400',
};

// ── Main component ────────────────────────────────────────────────────────────

export default function ScanHistoryPanel({ refreshKey = 0 }) {
  const [history, setHistory] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getScanHistory(20);
      setHistory(data);
    } catch (e) {
      setError(e.message ?? 'Failed to load scan history');
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial + re-load when a new scan completes (refreshKey bump)
  useEffect(() => { load(); }, [load, refreshKey]);

  const scans = history?.scans ?? [];

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl overflow-hidden">

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#263044] bg-[#131c27]">
        <div className="flex items-center gap-2.5">
          <History size={14} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">Scan History</h2>
          {history?.total != null && (
            <span className="text-[10px] text-gray-600">({history.total} records)</span>
          )}
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="text-gray-600 hover:text-gray-400 transition-colors"
          title="Refresh history"
        >
          <RefreshCw size={12} className={loading ? 'animate-spin text-blue-400' : ''} />
        </button>
      </div>

      <div className="p-4">
        {/* Loading */}
        {loading && !history && (
          <div className="flex items-center gap-2 text-xs text-gray-600 py-4 justify-center">
            <RefreshCw size={12} className="animate-spin" />
            Loading scan history…
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="flex items-center gap-2 text-xs text-amber-400/60 py-3">
            <AlertTriangle size={12} />
            {error}
          </div>
        )}

        {/* Empty */}
        {!loading && !error && scans.length === 0 && (
          <div className="text-xs text-gray-600 py-4 text-center">
            No scan records yet — click "Scan Institutional Setup" to create the first one.
          </div>
        )}

        {/* Records */}
        {scans.length > 0 && (
          <div className="space-y-2">
            {scans.map(s => <ScanRow key={s.id} scan={s} />)}
          </div>
        )}
      </div>
    </section>
  );
}

// ── Scan row ──────────────────────────────────────────────────────────────────

function ScanRow({ scan }) {
  const [expanded, setExpanded] = useState(false);
  const state   = scan.marketState ?? 'NO_TRADE';
  const signal  = scan.signal      ?? 'WAIT';
  const bias    = scan.institutionalBias;
  const conf    = scan.confidence  ?? 0;
  const SigCfg  = SIGNAL_ICON[signal] ?? SIGNAL_ICON.WAIT;
  const stateCls = STATE_CLS[state] ?? STATE_CLS.NO_TRADE;

  const ts = scan.createdAt
    ? new Date(scan.createdAt).toLocaleString(undefined, {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    : '—';

  const modeLabel = scan.scanMode === 'manual' ? 'Forced' : 'Auto';
  const modeCls   = scan.scanMode === 'manual'
    ? 'text-blue-400 bg-blue-900/20 border-blue-800/40'
    : 'text-gray-600 bg-gray-800/30 border-gray-700/30';

  return (
    <div className="border border-[#1e2535] rounded-lg overflow-hidden">
      {/* Summary row */}
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full text-left px-3 py-2.5 hover:bg-[#131c27] transition-colors"
      >
        <div className="flex items-center gap-2 flex-wrap">
          {/* Timestamp */}
          <div className="flex items-center gap-1 text-[10px] text-gray-600 min-w-[130px]">
            <Clock size={9} />
            {ts}
          </div>

          {/* Mode badge */}
          <span className={`text-[9px] px-1.5 py-0.5 rounded border ${modeCls}`}>
            {modeLabel}
          </span>

          {/* State badge */}
          <span className={`text-[9px] px-1.5 py-0.5 rounded border font-semibold ${stateCls}`}>
            {state.replace(/_/g, ' ')}
          </span>

          {/* Signal */}
          <div className="flex items-center gap-1">
            <SigCfg.Icon size={10} className={SigCfg.cls} />
            <span className={`text-[10px] font-bold ${SigCfg.cls}`}>{signal}</span>
          </div>

          {/* Bias */}
          {bias && (
            <span className={`text-[10px] ${BIAS_CLS[bias] ?? 'text-gray-400'}`}>{bias}</span>
          )}

          {/* Confidence */}
          <span className={`text-[10px] font-mono ml-auto ${conf >= 80 ? 'text-emerald-400' : conf >= 60 ? 'text-amber-400' : 'text-gray-500'}`}>
            {conf}%
          </span>
        </div>

        {/* Summary snippet */}
        {scan.summary && (
          <p className="text-[10px] text-gray-600 mt-1.5 leading-tight line-clamp-2 text-left">
            {scan.summary}
          </p>
        )}
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-[#1e2535] px-3 py-2.5 bg-[#0a0f17] space-y-2">

          {/* Key drivers */}
          {(scan.keyDrivers ?? []).length > 0 && (
            <div>
              <p className="text-[9px] text-gray-600 uppercase tracking-wider mb-1">Key Drivers</p>
              {scan.keyDrivers.map((d, i) => (
                <p key={i} className="text-[10px] text-gray-400 leading-tight">· {d}</p>
              ))}
            </div>
          )}

          {/* Blockers */}
          {(scan.blockers ?? []).length > 0 && (
            <div>
              <p className="text-[9px] text-gray-600 uppercase tracking-wider mb-1">Blockers</p>
              {scan.blockers.map((b, i) => (
                <p key={i} className="text-[10px] text-red-400/70 leading-tight">✗ {b}</p>
              ))}
            </div>
          )}

          {/* Model row */}
          {scan.model && (
            <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
              {[
                ['D1', scan.model.d1Bias],
                ['H4', scan.model.h4Bias],
                ['H1', scan.model.h1Bias],
                ['M15', scan.model.m15Structure],
              ].map(([lbl, val]) => val ? (
                <div key={lbl} className="flex gap-1.5 text-[10px]">
                  <span className="text-gray-600 w-7">{lbl}</span>
                  <span className="text-gray-400 truncate">{val}</span>
                </div>
              ) : null)}
            </div>
          )}

          {/* Action */}
          {scan.action && (
            <div className="text-[9px] text-blue-400/70 font-mono bg-blue-900/10 rounded px-2 py-1">
              Action: {scan.action.replace(/_/g, ' ')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
