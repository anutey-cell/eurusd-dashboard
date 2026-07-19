/**
 * VpTrapPanel
 *
 * Displays the current previous-day volume profile, the 4 candidate trap
 * zones with live states + scores, the top-scoring zone's playbook, and
 * a compact list of recent VP Trap signals.
 *
 * Polls /vp-trap/diagnostics every 30s.
 *
 * The panel intentionally shows "monitoring" bands (WATCH / DEVELOPING) as
 * dashboard-only rows — per brief spec, only score >= live_threshold is
 * presented as an actionable BUY/SELL.
 */
import { useState, useCallback, useEffect } from 'react';
import {
  TrendingUp, TrendingDown, RefreshCw, AlertTriangle, Activity,
  Target, Zap, Eye, Clock,
} from 'lucide-react';
import { getVpTrapDiagnostics, getVpTrapSignals } from '../services/api';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 30_000;

const BAND_CLS = {
  EXCEPTIONAL: 'bg-emerald-500/20 border-emerald-500/60 text-emerald-300',
  VALID:       'bg-emerald-500/15 border-emerald-500/50 text-emerald-300',
  DEVELOPING:  'bg-blue-500/15    border-blue-500/50    text-blue-300',
  WATCH:       'bg-amber-500/10   border-amber-500/40   text-amber-300',
  NO_SIGNAL:   'bg-gray-500/10    border-gray-600/40    text-gray-400',
};

const STATE_CLS = {
  LEVEL_DETECTED:  'text-gray-400',
  BREAKOUT_SEEN:   'text-amber-400',
  TRAP_ARMED:      'text-blue-400',
  WAITING_RETEST:  'text-blue-400',
  RETEST_ACTIVE:   'text-purple-400',
  TRIGGERED:       'text-emerald-400',
  INVALIDATED:     'text-red-400',
  EXPIRED:         'text-gray-500',
};

function fmtUsd(v) {
  if (v === null || v === undefined || v === '') return '—';
  return '$' + Number(v).toFixed(2);
}

function fmtNum(v, decimals = 2) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(decimals);
}

function Pill({ children, cls }) {
  return (
    <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${cls}`}>
      {children}
    </span>
  );
}

function ZoneRow({ zone, score, plan, isTop }) {
  const bandCls = BAND_CLS[score?.band] || BAND_CLS.NO_SIGNAL;
  const stateCls = STATE_CLS[zone.state] || STATE_CLS.LEVEL_DETECTED;
  const dirIcon = zone.level_side === 'SELL'
    ? <TrendingDown size={12} className="text-red-400" />
    : <TrendingUp   size={12} className="text-emerald-400" />;

  return (
    <div className={`border rounded-md px-3 py-2 ${
      isTop ? 'border-blue-500/50 bg-blue-500/5' : 'border-[#263044] bg-[#0d1117]'
    }`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {dirIcon}
          <span className="text-xs font-medium text-white">
            {zone.level_type}
          </span>
          <span className="text-xs text-gray-400">
            {fmtUsd(zone.reference_price)}
          </span>
          <span className={`text-[10px] uppercase tracking-wide ${stateCls}`}>
            {zone.state?.replace(/_/g, ' ')}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <Pill cls={bandCls}>
            {score?.total ?? 0}/100 · {score?.band || 'NO_SIGNAL'}
          </Pill>
          {score?.is_countertrend && (
            <Pill cls="bg-orange-500/15 border-orange-500/40 text-orange-300">
              CT
            </Pill>
          )}
          {score?.would_fire && (
            <Pill cls="bg-emerald-500/25 border-emerald-500/70 text-emerald-200">
              🪤 FIRE
            </Pill>
          )}
        </div>
      </div>

      {zone.state_reason && (
        <div className="text-[10px] text-gray-500 mt-1 truncate">
          {zone.state_reason}
        </div>
      )}

      {isTop && plan && (
        <div className="mt-2 pt-2 border-t border-blue-500/20 space-y-0.5">
          <div className="grid grid-cols-3 gap-2 text-[10px]">
            <div>
              <div className="text-gray-500">Entry</div>
              <div className="text-white font-medium">{fmtUsd(plan.entry)}</div>
            </div>
            <div>
              <div className="text-gray-500">SL</div>
              <div className="text-red-300 font-medium">{fmtUsd(plan.sl)}</div>
            </div>
            <div>
              <div className="text-gray-500">RR</div>
              <div className="text-emerald-300 font-medium">{fmtNum(plan.rr)}</div>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2 text-[10px]">
            <div>
              <div className="text-gray-500">TP1 (POC)</div>
              <div className="text-emerald-300">{fmtUsd(plan.tp1)}</div>
            </div>
            <div>
              <div className="text-gray-500">TP2 (VA)</div>
              <div className="text-emerald-300">{fmtUsd(plan.tp2)}</div>
            </div>
            <div>
              <div className="text-gray-500">TP3</div>
              <div className="text-emerald-300">{fmtUsd(plan.tp3)}</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function FactorGrid({ factors, factorMax }) {
  if (!factors) return null;
  const rows = Object.keys(factors).map(k => ({
    name: k, earned: factors[k], max: (factorMax || {})[k] || 0,
  })).filter(r => r.max > 0);
  return (
    <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
      {rows.map(r => {
        const pct = r.max > 0 ? r.earned / r.max : 0;
        const cls = pct >= 0.7 ? 'text-emerald-400'
                  : pct >= 0.4 ? 'text-amber-400'
                  : 'text-gray-500';
        return (
          <div key={r.name} className="flex items-center justify-between gap-1">
            <span className="text-gray-500 truncate">
              {r.name.replace(/_/g, ' ')}
            </span>
            <span className={`font-mono ${cls}`}>
              {r.earned}/{r.max}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function SignalRow({ sig }) {
  const time = sig.created_at ? new Date(sig.created_at).toLocaleString([], {
    dateStyle: 'short', timeStyle: 'short',
  }) : '—';
  const dirCls = sig.signal === 'SELL' ? 'text-red-300' : 'text-emerald-300';
  return (
    <div className="border border-[#263044] rounded-md px-2 py-1.5 bg-[#0d1117]">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`text-xs font-bold ${dirCls}`}>{sig.signal}</span>
          <span className="text-[10px] text-gray-400">{sig.setup_type}</span>
          <Pill cls="bg-blue-500/15 border-blue-500/40 text-blue-300">
            {sig.score_total}/100
          </Pill>
        </div>
        <span className="text-[10px] text-gray-500 whitespace-nowrap">{time}</span>
      </div>
      <div className="text-[10px] text-gray-500 mt-0.5 grid grid-cols-4 gap-2">
        <span>E {fmtUsd(sig.entry)}</span>
        <span>SL {fmtUsd(sig.stop_loss)}</span>
        <span>TP2 {fmtUsd(sig.tp2)}</span>
        <span>RR {fmtNum(sig.rr)}</span>
      </div>
      <div className="text-[10px] text-gray-500 mt-0.5 flex gap-2">
        <span>{sig.trap_side}</span>
        {sig.is_countertrend && (
          <span className="text-orange-400">CT</span>
        )}
        <span className="ml-auto">{sig.state}</span>
      </div>
    </div>
  );
}

export default function VpTrapPanel() {
  const [data, setData] = useState(null);
  const [signals, setSignals] = useState([]);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lastFetch, setLastFetch] = useState(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const [diag, sig] = await Promise.all([
        getVpTrapDiagnostics(),
        getVpTrapSignals({ limit: 8 }),
      ]);
      setData(diag);
      setSignals(sig.signals || []);
      setLastFetch(new Date());
    } catch (e) {
      setErr(e.message || 'Failed to load VP Trap data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);
  usePollInterval(fetchAll, POLL_MS);

  const zoneScores = data?.zone_scores || [];
  const topZone = zoneScores.reduce((best, cur) => {
    const s = cur.score?.total ?? 0;
    return (!best || s > (best.score?.total ?? 0)) ? cur : best;
  }, null);

  return (
    <div className="card">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <span className="text-lg">🪤</span>
          <span className="card-title">Volume Profile Trap Strategy</span>
          {data && (
            <Pill cls="bg-gray-500/15 border-gray-500/40 text-gray-400">
              tick_proxy
            </Pill>
          )}
        </div>
        <div className="flex items-center gap-2">
          {lastFetch && (
            <span className="text-[10px] text-gray-500 flex items-center gap-1">
              <Clock size={10} />
              {lastFetch.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
          <button
            onClick={fetchAll}
            disabled={loading}
            className="text-gray-400 hover:text-white transition-colors disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {err && (
        <div className="p-3 flex items-center gap-2 text-red-400 text-xs">
          <AlertTriangle size={14} /> {err}
        </div>
      )}

      {!data && !err && (
        <div className="p-4 text-center text-gray-500 text-xs">Loading…</div>
      )}

      {data && (
        <div className="p-3 space-y-3">
          {/* Profile summary */}
          <div className="grid grid-cols-2 xl:grid-cols-6 gap-2 text-[11px]">
            <div className="col-span-2 xl:col-span-6 text-[10px] text-gray-500">
              Profile date: <span className="text-white">{data.profile_date}</span> ·
              Current: <span className="text-white font-medium ml-1">{fmtUsd(data.current_price)}</span> ·
              ATR H1: <span className="text-white ml-1">{fmtNum(data.atr_h1)}</span> ·
              D1: <span className="text-white ml-1">{data.d1_bias || '—'}</span> ·
              H4: <span className="text-white ml-1">{data.h4_bias || '—'}</span> ·
              Live threshold: <span className="text-white ml-1">{data.live_threshold}</span>
            </div>
          </div>

          {/* Active zones */}
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-gray-300 flex items-center gap-1">
              <Target size={11} className="text-blue-400" />
              Active Zones (sorted by score)
            </div>
            {[...zoneScores]
              .sort((a, b) => (b.score?.total ?? 0) - (a.score?.total ?? 0))
              .map((zs, i) => (
              <ZoneRow key={zs.zone.zone_id} zone={zs.zone} score={zs.score}
                       plan={zs.trade_plan} isTop={zs === topZone} />
            ))}
          </div>

          {/* Top zone factor breakdown */}
          {topZone && topZone.score && (
            <div className="border border-[#263044] rounded-md p-2 bg-[#0d1117]">
              <div className="text-xs font-medium text-gray-300 mb-1.5 flex items-center gap-1">
                <Activity size={11} className="text-blue-400" />
                Top zone factor breakdown
              </div>
              <FactorGrid
                factors={topZone.score.factors}
                factorMax={topZone.score.factor_max}
              />
              {topZone.score.reason_qualifies && (
                <div className="text-[10px] text-gray-500 mt-2 pt-2 border-t border-[#263044]">
                  {topZone.score.reason_qualifies}
                </div>
              )}
            </div>
          )}

          {/* Recent signals */}
          <div className="space-y-1">
            <div className="text-xs font-medium text-gray-300 flex items-center gap-1">
              <Zap size={11} className="text-emerald-400" />
              Recent VP Trap Signals ({signals.length})
            </div>
            {signals.length === 0 ? (
              <div className="text-[10px] text-gray-500 italic px-2 py-1">
                No fired signals yet. When a zone reaches score ≥ {data.live_threshold},
                a Telegram alert + persisted signal will appear here.
              </div>
            ) : (
              <div className="space-y-1">
                {signals.map(sig => <SignalRow key={sig.id} sig={sig} />)}
              </div>
            )}
          </div>

          <div className="text-[10px] text-gray-500 pt-1 border-t border-[#263044]">
            <Eye size={10} className="inline mr-1 -mt-0.5" />
            Signal-only. No MT5 execution. Complements the mandate strategist —
            never suppresses its signals.
          </div>
        </div>
      )}
    </div>
  );
}
