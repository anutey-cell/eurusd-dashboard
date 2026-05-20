/**
 * AutonomousExecutorPanel
 *
 * Read-only operator dashboard for the autonomous learning-mode executor.
 *
 * Shows:
 *   - Master state (ON / OFF) and the chain of switches required
 *   - Daily trade counter (X / 3) with progress bar
 *   - 3-layer confirmation status (scanner / predictor / killzone)
 *   - Last attempt outcome (fired? blocked? why?)
 *   - "Preview" button — runs the gate gauntlet without firing
 *
 * Polls every 20 s. Never executes anything directly.
 */
import { useState, useEffect, useCallback } from 'react';
import { usePollInterval } from '../hooks/usePollInterval';
import {
  Zap, ShieldCheck, ShieldOff, AlertTriangle, CheckCircle, XCircle,
  Activity, Eye, RefreshCw, Lock, Unlock, Target,
} from 'lucide-react';
import { getAutonomousStatus, previewAutonomous } from '../services/api';
import {
  formatKenyaTime, formatKenyaDateTime, utcWindowToKenya, KENYA_LABEL,
} from '../utils/time';

const POLL_MS = 20_000;

const STATE_CFG = {
  on:  { cls: 'bg-emerald-500/20 border-emerald-500/60 text-emerald-300', Icon: ShieldCheck,  label: 'AUTO ON' },
  off: { cls: 'bg-slate-700/40   border-slate-600/60   text-slate-300',   Icon: ShieldOff,    label: 'AUTO OFF' },
  blocked: { cls: 'bg-amber-500/20  border-amber-500/60  text-amber-300',  Icon: AlertTriangle, label: 'BLOCKED' },
};

const LAYER_LABELS = {
  scanner:   'Scanner',
  predictor: 'Predictor',
  killzone:  'Killzone',
};

function statusFromStatus(status) {
  const cfg = status?.config ?? {};
  if (!cfg.enabled) return 'off';
  if (!status?.master_switch?.ok) return 'blocked';
  return 'on';
}

// ── Sub: master switch chain ────────────────────────────────────────────────

function SwitchChain({ config, master }) {
  const checks = [
    { label: 'AUTO_EXECUTION_ENABLED',  ok: !!config.enabled              },
    { label: 'DATA_MODE=live',          ok: config.data_mode === 'live'   },
    { label: 'MT5_EXECUTION_ENABLED',   ok: !!config.mt5_execution_enabled},
    { label: 'LIVE_TRADING_AUTHORIZED', ok: !!config.live_authorized      },
    { label: 'Kill switch inactive',    ok: !!master?.ok                  },
  ];
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
      {checks.map(c => (
        <div
          key={c.label}
          className={`flex items-center gap-1.5 text-[10px] px-2 py-1 rounded border ${
            c.ok
              ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
              : 'bg-red-500/10     border-red-500/30     text-red-300'
          }`}
        >
          {c.ok ? <CheckCircle size={10} /> : <XCircle size={10} />}
          <span className="font-mono truncate">{c.label}</span>
        </div>
      ))}
    </div>
  );
}

// ── Sub: daily count bar ────────────────────────────────────────────────────

function DailyCounter({ daily }) {
  const pct = daily.limit ? Math.min(100, (daily.trades_today / daily.limit) * 100) : 0;
  const barCls = daily.at_cap
    ? 'bg-red-500'
    : daily.trades_today >= daily.limit - 1
      ? 'bg-amber-500'
      : 'bg-emerald-500';
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400 uppercase tracking-widest text-[10px]">Daily Trades</span>
        <span className="font-mono font-bold text-white">
          {daily.trades_today} <span className="opacity-60">/ {daily.limit}</span>
        </span>
      </div>
      <div className="h-2 bg-slate-800 rounded overflow-hidden">
        <div className={`h-full ${barCls} transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <div className="text-[10px] text-gray-500">
        {daily.at_cap
          ? `Daily ceiling reached — no more trades until 03:00 ${KENYA_LABEL} (00:00 UTC).`
          : `${daily.trades_remaining} trade${daily.trades_remaining === 1 ? '' : 's'} remaining today`}
      </div>
    </div>
  );
}

// ── Sub: confirmation layer card ────────────────────────────────────────────

function LayerCard({ name, data, blockedHere }) {
  if (!data) {
    return (
      <div className="rounded border border-slate-700 p-2 bg-slate-800/40">
        <div className="text-[10px] uppercase tracking-widest text-gray-500">{LAYER_LABELS[name]}</div>
        <div className="text-xs text-gray-500 mt-1">No data</div>
      </div>
    );
  }
  const ok = !blockedHere;
  const cls = ok
    ? 'border-emerald-500/40 bg-emerald-500/10'
    : 'border-red-500/40     bg-red-500/10';

  // Pick the two most informative fields per layer
  let lines = [];
  if (name === 'scanner') {
    lines = [
      `state ${data.marketState || '—'}  ·  ${data.signal || '—'}`,
      `score ${data.qualityScore ?? '—'}`,
    ];
  } else if (name === 'predictor') {
    lines = [
      `${data.band || '—'}  ·  ${data.direction || '—'}`,
      `aligned ${data.alignedLayers ?? '—'}/7  ·  score ${data.score ?? '—'}`,
    ];
  } else if (name === 'killzone') {
    const eatWindow = utcWindowToKenya(data.window_utc);
    lines = [
      `${data.label || data.killzone || '—'}  ${eatWindow ? `${eatWindow} ${KENYA_LABEL}` : ''}`,
      `${data.posture || '—'}  ·  edge ${data.edge_score ?? '—'}/100`,
    ];
  }

  return (
    <div className={`rounded border p-2 ${cls}`}>
      <div className="flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-widest font-bold">{LAYER_LABELS[name]}</div>
        {ok ? <CheckCircle size={11} className="text-emerald-400" />
            : <XCircle     size={11} className="text-red-400" />}
      </div>
      {lines.map((l, i) => (
        <div key={i} className="text-[10px] font-mono opacity-90 leading-tight mt-1">{l}</div>
      ))}
    </div>
  );
}

// ── Sub: last attempt summary ───────────────────────────────────────────────

function LastAttempt({ last }) {
  if (!last) {
    return (
      <div className="text-xs text-gray-500 italic">
        No execution cycle has run yet — waiting for first loop tick.
      </div>
    );
  }
  if (last.fired) {
    return (
      <div className="rounded border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs">
        <div className="flex items-center gap-2 text-emerald-300 font-bold">
          <Zap size={12} />
          ORDER FIRED · {last.signal} XAU/USD · {last.lot_size} lot
        </div>
        <div className="text-[10px] text-emerald-300/80 font-mono mt-1">
          Ticket #{last.ticket} · Entry {last.entry} · SL {last.stop_loss} · TP {last.take_profit}
        </div>
        <div className="text-[10px] text-gray-500 mt-1">
          {formatKenyaDateTime(last.ts)} {KENYA_LABEL}
        </div>
      </div>
    );
  }
  if (last.blocking_reason) {
    return (
      <div className="rounded border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
        <div className="flex items-center gap-2 text-amber-300 font-bold">
          <Lock size={12} />
          BLOCKED at {last.blocking_layer || 'unknown'}
        </div>
        <div className="text-[10px] text-amber-200/90 mt-1 font-mono">
          {last.blocking_reason}
        </div>
        <div className="text-[10px] text-gray-500 mt-1">
          {formatKenyaDateTime(last.ts)} {KENYA_LABEL}
        </div>
      </div>
    );
  }
  return null;
}

// ── Main panel ──────────────────────────────────────────────────────────────

export default function AutonomousExecutorPanel() {
  const [status,  setStatus]  = useState(null);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);

  const load = useCallback(async () => {
    try {
      const s = await getAutonomousStatus();
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Status unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  const runPreview = useCallback(async () => {
    try {
      setPreview({ loading: true });
      const p = await previewAutonomous();
      setPreview(p);
    } catch (e) {
      setPreview({ error: e?.message ?? 'Preview failed' });
    }
  }, []);

  usePollInterval(load, POLL_MS);

  const state    = statusFromStatus(status);
  const stateCfg = STATE_CFG[state];

  const last         = status?.last_attempt;
  const blockedLayer = last?.blocking_layer;
  const confs        = last?.confirmations ?? {};

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Zap size={16} className="text-amber-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Autonomous Live Executor
          </h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            Learning-mode trading · max {status?.config?.max_lot ?? 0.05} lot · max{' '}
            {status?.config?.max_trades_per_day ?? 3} trades/day
          </span>
        </div>

        <div className="flex items-center gap-2">
          <div className={`flex items-center gap-1.5 px-2 py-1 rounded border ${stateCfg.cls}`}>
            <stateCfg.Icon size={11} />
            <span className="text-[10px] font-bold uppercase tracking-widest">{stateCfg.label}</span>
          </div>
          <button
            onClick={runPreview}
            className="flex items-center gap-1 px-2 py-1 rounded bg-[#161b27] border border-[#263044] text-gray-300 hover:text-white text-xs"
          >
            <Eye size={11} />
            Preview
          </button>
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1 px-2 py-1 rounded bg-[#161b27] border border-[#263044] text-gray-300 hover:text-white text-xs"
          >
            <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded p-3 text-xs text-red-300 flex items-center gap-2">
          <AlertTriangle size={12} />
          {error}
        </div>
      )}

      {status && (
        <>
          {/* Switch chain */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-2">
              Master Switch Chain (ALL must be green to trade)
            </div>
            <SwitchChain config={status.config} master={status.master_switch} />
          </div>

          {/* Daily counter + risk caps */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2 border-t border-[#1c2333]">
            <DailyCounter daily={status.daily} />
            <div>
              <div className="text-[10px] uppercase tracking-widest text-gray-500">Lot Cap</div>
              <div className="text-2xl font-mono font-bold text-white">
                {status.config.max_lot}
              </div>
              <div className="text-[10px] text-gray-500">
                Hard ceiling — never exceeded regardless of risk math
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-widest text-gray-500">Confirmations Needed</div>
              <div className="text-sm text-gray-200 mt-1">
                <Lock size={10} className="inline mr-1 text-amber-400" />
                Scanner ≥ {status.thresholds.scanner_min_score} ·
                {' '}Predictor ∈ {status.thresholds.predictor_bands.join('/')} ·
                {' '}KZ ≥ {status.thresholds.killzone_min_edge}
              </div>
              <div className="text-[10px] text-gray-500 mt-1">
                If any layer disagrees, no trade. By design.
              </div>
            </div>
          </div>

          {/* Last attempt + 3-layer status */}
          <div className="pt-2 border-t border-[#1c2333] space-y-3">
            <div className="text-[10px] uppercase tracking-widest text-gray-500">
              Last Evaluation Cycle
            </div>
            <LastAttempt last={last} />
            {(confs.scanner || confs.predictor || confs.killzone) && (
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                <LayerCard name="scanner"   data={confs.scanner}   blockedHere={blockedLayer === 'scanner'} />
                <LayerCard name="predictor" data={confs.predictor} blockedHere={blockedLayer === 'predictor'} />
                <LayerCard name="killzone"  data={confs.killzone}  blockedHere={blockedLayer === 'killzone'} />
              </div>
            )}
          </div>

          {/* Preview result */}
          {preview && !preview.loading && (
            <div className="pt-2 border-t border-[#1c2333]">
              <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-1">
                Dry-Run Preview
              </div>
              {preview.error ? (
                <div className="text-xs text-red-300">{preview.error}</div>
              ) : preview.would_fire ? (
                <div className="text-xs text-emerald-300 font-bold flex items-center gap-1">
                  <CheckCircle size={11} />
                  Would fire {preview.signal} XAU/USD at {preview.entry} (lot ≤ {preview.max_lot})
                </div>
              ) : (
                <div className="text-xs text-amber-300">
                  Would NOT fire — blocked at <b>{preview.blocking_layer}</b>: {preview.blocking_reason}
                </div>
              )}
            </div>
          )}

          {/* Footer */}
          <div className="flex items-center justify-between text-[10px] text-gray-600 pt-2 border-t border-[#1c2333]">
            <span>
              <Activity size={10} className="inline" /> Polls every {(POLL_MS / 1000)}s · Loop every{' '}
              {status.config.interval_sec}s
            </span>
            <span>Updated {formatKenyaTime(status.generated_at)} {KENYA_LABEL}</span>
          </div>
        </>
      )}
    </div>
  );
}
