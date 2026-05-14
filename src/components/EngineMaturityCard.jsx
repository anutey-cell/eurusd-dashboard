/**
 * EngineMaturityCard
 *
 * Displays the adaptive learning engine's self-assessment:
 *   - Maturity score (0–100), grows as trade outcomes are logged
 *   - Per-component accuracy: how often each ICT gate predicted correctly
 *   - Learned scoring weights vs baseline
 *   - MT5 bridge status (live vs synthetic data)
 *   - Overall win rate from logged outcomes
 *
 * Calls GET /api/v1/engine/status on mount + every 5 min.
 */
import { useState, useEffect, useCallback } from 'react';
import {
  Brain, Wifi, WifiOff, RefreshCw, TrendingUp, TrendingDown,
  AlertTriangle, CheckCircle, Minus, ChevronDown, ChevronUp,
  Activity, Zap,
} from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const REFRESH_MS = 5 * 60 * 1000;   // 5 min

// ── helpers ───────────────────────────────────────────────────────────────────

async function fetchEngineStatus(pair = 'all') {
  const res = await fetch(`${API_BASE}/api/v1/engine/status?pair=${pair}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const json = await res.json();
  return json.data;
}

async function triggerLearn() {
  const res = await fetch(`${API_BASE}/api/v1/engine/learn`, { method: 'POST' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function MaturityRing({ score, label }) {
  const color =
    score >= 75 ? 'text-emerald-400' :
    score >= 50 ? 'text-blue-400' :
    score >= 25 ? 'text-amber-400' :
    'text-gray-500';

  const bgColor =
    score >= 75 ? 'stroke-emerald-500' :
    score >= 50 ? 'stroke-blue-500' :
    score >= 25 ? 'stroke-amber-500' :
    'stroke-gray-600';

  const r = 28;
  const circ = 2 * Math.PI * r;
  const offset = circ - (score / 100) * circ;

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative w-20 h-20">
        <svg className="w-20 h-20 -rotate-90" viewBox="0 0 72 72">
          <circle cx="36" cy="36" r={r} fill="none" stroke="#263044" strokeWidth="6" />
          <circle
            cx="36" cy="36" r={r} fill="none"
            strokeWidth="6" strokeLinecap="round"
            strokeDasharray={circ}
            strokeDashoffset={offset}
            className={`transition-all duration-1000 ${bgColor}`}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className={`text-lg font-black font-mono ${color}`}>{score}</span>
        </div>
      </div>
      <div className="text-[10px] text-gray-500 uppercase tracking-wider text-center">{label}</div>
    </div>
  );
}

function ComponentBar({ name, baseWeight, learnedWeight, winRate, nSamples, calibrated }) {
  const pct = Math.round(winRate * 100);
  const delta = learnedWeight - baseWeight;
  const barColor =
    pct >= 65 ? 'bg-emerald-500' :
    pct >= 50 ? 'bg-blue-500' :
    pct >= 40 ? 'bg-amber-500' :
    'bg-red-500';

  return (
    <div className="py-2 border-b border-[#1e2535] last:border-0">
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-2">
          {calibrated
            ? <CheckCircle size={11} className="text-emerald-400 flex-shrink-0" />
            : <Minus size={11} className="text-gray-600 flex-shrink-0" />
          }
          <span className="text-xs text-gray-300">{name}</span>
        </div>
        <div className="flex items-center gap-3 text-[10px]">
          {calibrated ? (
            <span className={`font-mono ${pct >= 55 ? 'text-emerald-400' : pct >= 45 ? 'text-amber-400' : 'text-red-400'}`}>
              {pct}% win
            </span>
          ) : (
            <span className="text-gray-600">{nSamples} samples</span>
          )}
          <span className="text-gray-600 font-mono w-10 text-right">
            {delta > 0 ? '+' : ''}{delta !== 0 ? delta : '='}{delta !== 0 ? '' : ' base'}
          </span>
          <span className={`font-mono w-6 text-right font-bold ${
            learnedWeight > baseWeight ? 'text-emerald-400' :
            learnedWeight < baseWeight ? 'text-red-400' : 'text-gray-400'
          }`}>{learnedWeight}</span>
        </div>
      </div>
      <div className="w-full h-1.5 bg-[#263044] rounded-full overflow-hidden">
        {calibrated ? (
          <div
            className={`h-full rounded-full transition-all ${barColor}`}
            style={{ width: `${pct}%` }}
          />
        ) : (
          <div className="h-full rounded-full bg-gray-700 w-1/4 animate-pulse" />
        )}
      </div>
      {!calibrated && nSamples > 0 && (
        <div className="text-[10px] text-gray-600 mt-0.5">
          {nSamples}/{15} outcomes needed to calibrate
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function EngineMaturityCard() {
  const [status,   setStatus]   = useState(null);
  const [loading,  setLoading]  = useState(true);
  const [error,    setError]    = useState(null);
  const [learning, setLearning] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchEngineStatus('all');
      setStatus(data);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  async function handleLearn() {
    setLearning(true);
    try {
      await triggerLearn();
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setLearning(false);
    }
  }

  const mat    = status?.maturity   ?? {};
  const bridge = status?.bridge     ?? {};
  const comps  = status?.components ?? [];
  const pairs  = status?.pair_breakdown ?? {};
  const wts    = status?.weights    ?? {};

  const matScore = mat.score        ?? 0;
  const matLevel = mat.level        ?? 'Initialising';
  const nOutcomes = mat.n_outcomes  ?? 0;
  const winRate   = mat.overall_win_rate ?? 0;
  const bridgeUp  = bridge.connected  ?? false;

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Brain size={15} className="text-purple-400" />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
            Adaptive Learning Engine
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {bridgeUp
            ? <span className="flex items-center gap-1 text-[10px] text-emerald-400 bg-emerald-900/20 border border-emerald-800/30 px-2 py-0.5 rounded-full">
                <Wifi size={9} /> Live MT5 Feed
              </span>
            : <span className="flex items-center gap-1 text-[10px] text-amber-400/70 bg-amber-900/10 border border-amber-800/20 px-2 py-0.5 rounded-full">
                <WifiOff size={9} /> Synthetic Data
              </span>
          }
        </div>
      </div>

      {/* ── Maturity ring + key stats ───────────────────────────────────── */}
      <div className="flex items-center gap-4 bg-[#131c27] rounded-xl p-4">
        <MaturityRing score={matScore} label={matLevel} />

        <div className="flex-1 grid grid-cols-3 gap-2">
          <div className="bg-[#1e2535] rounded-lg p-2.5 text-center">
            <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Outcomes</div>
            <div className="font-mono text-sm font-bold text-white">{nOutcomes}</div>
            <div className="text-[10px] text-gray-600">/{mat.target ?? 100} target</div>
          </div>
          <div className="bg-[#1e2535] rounded-lg p-2.5 text-center">
            <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Win Rate</div>
            <div className={`font-mono text-sm font-bold ${
              winRate >= 0.6 ? 'text-emerald-400' :
              winRate >= 0.5 ? 'text-blue-400' : 'text-red-400'
            }`}>
              {nOutcomes ? `${Math.round(winRate * 100)}%` : '—'}
            </div>
          </div>
          <div className="bg-[#1e2535] rounded-lg p-2.5 text-center">
            <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Calibrated</div>
            <div className={`text-sm font-bold ${mat.calibrated ? 'text-emerald-400' : 'text-gray-500'}`}>
              {mat.calibrated ? 'YES' : 'NO'}
            </div>
          </div>
        </div>
      </div>

      {/* ── Pair breakdown ──────────────────────────────────────────────── */}
      {(pairs.eurusd?.n_outcomes > 0 || pairs.xauusd?.n_outcomes > 0) && (
        <div className="grid grid-cols-2 gap-2">
          {[
            { code: 'eurusd', label: 'EUR/USD' },
            { code: 'xauusd', label: 'XAU/USD' },
          ].map(({ code, label }) => {
            const p = pairs[code] ?? {};
            return (
              <div key={code} className="bg-[#1e2535] rounded-lg p-2.5">
                <div className="text-[10px] text-gray-500 mb-1">{label}</div>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-400">{p.n_outcomes ?? 0} trades</span>
                  <span className={`text-xs font-bold font-mono ${
                    (p.win_rate ?? 0) >= 0.6 ? 'text-emerald-400' :
                    (p.win_rate ?? 0) >= 0.5 ? 'text-blue-400' : 'text-gray-500'
                  }`}>
                    {p.n_outcomes ? `${Math.round((p.win_rate ?? 0) * 100)}%` : '—'}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── Component accuracy (collapsible) ────────────────────────────── */}
      {comps.length > 0 && (
        <>
          <button
            onClick={() => setExpanded(e => !e)}
            className="w-full flex items-center justify-between text-[10px] text-slate-500 hover:text-slate-400 transition-colors py-1"
          >
            <span className="uppercase tracking-widest font-semibold">
              Component Accuracy &amp; Weight Calibration
            </span>
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>

          {expanded && (
            <div className="bg-[#131c27] rounded-xl p-3">
              {/* Column headers */}
              <div className="flex items-center justify-between mb-2 text-[9px] text-gray-600 uppercase tracking-wider">
                <span>Component</span>
                <div className="flex items-center gap-3">
                  <span className="w-12 text-right">Win Rate</span>
                  <span className="w-10 text-right">Δ Wt</span>
                  <span className="w-6 text-right">Wt</span>
                </div>
              </div>
              {comps.map(c => (
                <ComponentBar
                  key={c.name}
                  name={c.name}
                  baseWeight={c.base_weight}
                  learnedWeight={c.learned_weight}
                  winRate={c.win_rate}
                  nSamples={c.n_samples}
                  calibrated={c.calibrated}
                />
              ))}

              {/* Weight totals */}
              <div className="mt-2 pt-2 border-t border-[#263044] flex justify-between text-[10px]">
                <span className="text-gray-600">Baseline total</span>
                <span className="font-mono text-gray-500">
                  {comps.reduce((s, c) => s + c.base_weight, 0)} pts
                </span>
              </div>
              <div className="flex justify-between text-[10px]">
                <span className="text-gray-600">Learned total</span>
                <span className={`font-mono ${
                  comps.reduce((s, c) => s + c.learned_weight, 0) === 100
                    ? 'text-emerald-400' : 'text-amber-400'
                }`}>
                  {comps.reduce((s, c) => s + c.learned_weight, 0)} pts
                </span>
              </div>
            </div>
          )}
        </>
      )}

      {/* ── Empty state ─────────────────────────────────────────────────── */}
      {!loading && nOutcomes === 0 && (
        <div className="bg-[#131c27] rounded-xl p-4 text-center space-y-1">
          <Activity size={18} className="text-purple-400/40 mx-auto" />
          <p className="text-xs text-gray-500">
            No outcomes logged yet
          </p>
          <p className="text-[10px] text-gray-600">
            Log trade results in the journal below to start learning
          </p>
        </div>
      )}

      {/* ── Error ───────────────────────────────────────────────────────── */}
      {error && (
        <div className="flex items-center gap-2 text-xs text-red-400 bg-red-900/20 border border-red-800/40 rounded-lg px-3 py-2">
          <AlertTriangle size={12} />
          {error}
        </div>
      )}

      {/* ── Footer actions ───────────────────────────────────────────────── */}
      <div className="flex items-center justify-between pt-1">
        <p className="text-[10px] text-gray-600">
          {mat.calibrated
            ? 'Weights auto-adjusted from logged outcomes'
            : `Need ${Math.max(0, 15 - nOutcomes)} more outcomes to begin calibrating`}
        </p>
        <button
          onClick={handleLearn}
          disabled={learning}
          className="flex items-center gap-1.5 text-[10px] text-purple-400 hover:text-purple-300 disabled:opacity-50 transition-colors"
        >
          <RefreshCw size={10} className={learning ? 'animate-spin' : ''} />
          {learning ? 'Learning…' : 'Run Learning Cycle'}
        </button>
      </div>
    </section>
  );
}
