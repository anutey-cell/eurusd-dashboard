import { useState, useEffect } from 'react';
import { Shield, Lock, AlertTriangle, CheckCircle, XCircle, Zap } from 'lucide-react';
import { getExecutionStatus, triggerKillSwitch } from '../services/api';

// ── Safety requirements checklist ─────────────────────────────────────────────

const REQUIREMENTS = [
  {
    id: 'backtests',
    label: 'Minimum 100 backtested setups completed',
    detail: 'Run the backtester across at least 100 historical signals before enabling live execution.',
  },
  {
    id: 'paper',
    label: 'Minimum 50 paper-traded signals reviewed',
    detail: 'Manually review 50 paper trades to validate signal quality in real-time conditions.',
  },
  {
    id: 'expectancy',
    label: 'Positive expectancy confirmed in backtests',
    detail: 'Both in-sample and out-of-sample expectancy must be positive with the chosen parameters.',
  },
  {
    id: 'drawdown',
    label: 'Maximum drawdown within acceptable range',
    detail: 'Out-of-sample max drawdown must not exceed your personal risk tolerance before going live.',
  },
  {
    id: 'broker',
    label: 'Broker SDK integrated and tested',
    detail: 'Complete the broker_provider.py integration and verify with a practice account first.',
  },
];

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusBadge({ enabled, killActive }) {
  if (killActive) {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-red-900/40 text-red-400 border border-red-700/50">
        <Zap size={10} className="fill-current" />
        KILL SWITCH ACTIVE
      </span>
    );
  }
  if (enabled) {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-900/40 text-emerald-400 border border-emerald-700/50">
        <CheckCircle size={10} />
        ENABLED
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">
      <Lock size={10} />
      DISABLED
    </span>
  );
}

function RequirementRow({ label, detail }) {
  return (
    <li className="flex items-start gap-2.5 text-xs">
      <XCircle size={13} className="text-slate-600 mt-0.5 flex-shrink-0" />
      <div>
        <span className="text-slate-300">{label}</span>
        <p className="text-slate-500 mt-0.5 leading-relaxed">{detail}</p>
      </div>
    </li>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function ExecutionPanel() {
  const [status, setStatus]             = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [killConfirm, setKillConfirm]   = useState(false);
  const [killing, setKilling]           = useState(false);
  const [killError, setKillError]       = useState(null);

  useEffect(() => {
    setLoadingStatus(true);
    getExecutionStatus()
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(() => setLoadingStatus(false));
  }, []);

  async function handleKillSwitch() {
    if (!killConfirm) {
      setKillConfirm(true);
      return;
    }
    setKilling(true);
    setKillError(null);
    try {
      const result = await triggerKillSwitch();
      setStatus(prev => ({
        ...prev,
        enabled:          false,
        killSwitchActive: true,
        killSwitchAt:     result.triggeredAt,
        note:             result.message,
      }));
    } catch (err) {
      setKillError(err?.message ?? 'Kill switch failed');
    } finally {
      setKilling(false);
      setKillConfirm(false);
    }
  }

  const isEnabled   = status?.enabled ?? false;
  const killActive  = status?.killSwitchActive ?? false;
  const provider    = status?.provider ?? '—';

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-5">

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Shield size={16} className="text-slate-400" />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
            Broker Execution
          </h2>
        </div>
        {loadingStatus
          ? <span className="text-[10px] text-slate-600 animate-pulse">Loading…</span>
          : <StatusBadge enabled={isEnabled} killActive={killActive} />
        }
      </div>

      {/* ── Disabled notice ──────────────────────────────────────────────────── */}
      <div className="rounded-lg border border-amber-800/40 bg-amber-900/10 p-4 flex gap-3">
        <AlertTriangle size={15} className="text-amber-500 flex-shrink-0 mt-0.5" />
        <div className="text-xs text-amber-300/80 leading-relaxed space-y-1">
          <p className="font-semibold text-amber-300">Manual confirmation mode only</p>
          <p>
            Execution can only be enabled after a minimum of 100 backtested setups,
            50 paper-traded signals, positive expectancy, and acceptable drawdown.
          </p>
          <p className="text-amber-400/60">
            Set <code className="font-mono bg-black/30 px-1 py-0.5 rounded">BROKER_EXECUTION_ENABLED=true</code> in{' '}
            <code className="font-mono bg-black/30 px-1 py-0.5 rounded">.env</code> only when all requirements below are met.
          </p>
        </div>
      </div>

      {/* ── Provider info ────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="bg-[#131c27] rounded-lg p-3 space-y-0.5">
          <p className="text-slate-500 uppercase tracking-wider text-[10px]">Provider</p>
          <p className="text-slate-200 font-mono capitalize">{provider}</p>
        </div>
        <div className="bg-[#131c27] rounded-lg p-3 space-y-0.5">
          <p className="text-slate-500 uppercase tracking-wider text-[10px]">Max Risk / Trade</p>
          <p className="text-slate-200 font-mono">0.25%</p>
        </div>
      </div>

      {/* ── Requirements checklist ───────────────────────────────────────────── */}
      <div>
        <p className="text-[10px] uppercase tracking-widest text-slate-500 mb-3">
          Safety Requirements (all must be satisfied)
        </p>
        <ul className="space-y-3">
          {REQUIREMENTS.map(r => (
            <RequirementRow key={r.id} label={r.label} detail={r.detail} />
          ))}
        </ul>
      </div>

      {/* ── Disabled place-order placeholder ────────────────────────────────── */}
      <div className="rounded-lg border border-dashed border-slate-700 bg-[#0a0f16] p-4 text-center space-y-2">
        <Lock size={18} className="text-slate-600 mx-auto" />
        <p className="text-xs text-slate-500">
          Place-order button will appear here once execution is enabled and the
          broker integration is completed.
        </p>
      </div>

      {/* ── Emergency kill switch ────────────────────────────────────────────── */}
      {!killActive && (
        <div className="border-t border-[#263044] pt-4 space-y-2">
          <p className="text-[10px] text-slate-600 uppercase tracking-widest">Emergency Controls</p>
          {killError && (
            <p className="text-xs text-red-400">{killError}</p>
          )}
          {killConfirm ? (
            <div className="flex gap-2">
              <button
                onClick={handleKillSwitch}
                disabled={killing}
                className="flex-1 py-2 text-xs font-semibold rounded-lg bg-red-700 hover:bg-red-600 text-white disabled:opacity-50 transition-colors"
              >
                {killing ? 'Activating…' : 'Confirm: Block All Execution'}
              </button>
              <button
                onClick={() => setKillConfirm(false)}
                className="px-4 py-2 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition-colors"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={handleKillSwitch}
              className="w-full py-2 text-xs font-semibold rounded-lg border border-red-800/50 text-red-500 hover:bg-red-900/20 transition-colors flex items-center justify-center gap-2"
            >
              <Zap size={12} />
              Emergency Kill Switch
            </button>
          )}
          <p className="text-[10px] text-slate-600 text-center">
            Immediately blocks all broker execution. Requires server restart to reset.
          </p>
        </div>
      )}

      {/* ── Kill switch active banner ────────────────────────────────────────── */}
      {killActive && (
        <div className="rounded-lg border border-red-700/60 bg-red-900/20 p-3 flex gap-2 items-center">
          <Zap size={14} className="text-red-400 flex-shrink-0" />
          <p className="text-xs text-red-300">
            Kill switch active. All execution is blocked.
            {status?.killSwitchAt && (
              <> Triggered at <span className="font-mono">{status.killSwitchAt}</span>.</>
            )}{' '}
            Restart the server to reset.
          </p>
        </div>
      )}

    </section>
  );
}
