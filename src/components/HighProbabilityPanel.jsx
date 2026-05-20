/**
 * HighProbabilityPanel
 *
 * Decision-support card combining 5 evidence layers (technical, fundamental,
 * news, volatility, sentiment) into a single probability score with
 * factor breakdown.
 *
 * Designed for MANUAL trade execution — the user reviews the prediction,
 * sees which layers align, and decides whether to take the trade.
 *
 * Auto-refreshes every 60 seconds. Updates trade plan when scanner fires
 * a SIGNAL_READY state with full entry/SL/TP geometry.
 */
import { useState, useEffect, useCallback } from 'react';
import {
  Brain, TrendingUp, TrendingDown, Minus, RefreshCw, CheckCircle,
  XCircle, AlertTriangle, Award, Layers, Globe, Newspaper, Activity, Users,
} from 'lucide-react';
import { getHighProbabilityPrediction } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 60_000;

const BAND_CFG = {
  STRONG:   { cls: 'from-emerald-700/40 to-emerald-900/20 border-emerald-600/60 text-emerald-300', label: 'STRONG PROBABILITY' },
  MODERATE: { cls: 'from-blue-700/40    to-blue-900/20    border-blue-600/60    text-blue-300',    label: 'MODERATE PROBABILITY' },
  WEAK:     { cls: 'from-amber-700/40   to-amber-900/20   border-amber-600/60   text-amber-300',   label: 'WEAK PROBABILITY' },
  AVOID:    { cls: 'from-red-700/40     to-red-900/20     border-red-600/60     text-red-300',     label: 'AVOID — STAND ASIDE' },
};

const STATUS_CFG = {
  GREEN:  { cls: 'border-emerald-500/50 bg-emerald-900/20 text-emerald-300', dot: 'bg-emerald-400' },
  YELLOW: { cls: 'border-amber-500/50   bg-amber-900/20   text-amber-300',   dot: 'bg-amber-400' },
  RED:    { cls: 'border-red-500/50     bg-red-900/20     text-red-300',     dot: 'bg-red-400' },
};

const LAYER_ICONS = {
  technical:   Layers,
  fundamental: Globe,
  news:        Newspaper,
  volatility:  Activity,
  sentiment:   Users,
};

const DIRECTION_CFG = {
  BUY:  { Icon: TrendingUp,   cls: 'text-emerald-400' },
  SELL: { Icon: TrendingDown, cls: 'text-red-400' },
  WAIT: { Icon: Minus,        cls: 'text-gray-400' },
  NEUTRAL: { Icon: Minus,     cls: 'text-gray-500' },
};

// ── Sub-components ────────────────────────────────────────────────────────────

function LayerCard({ layer }) {
  const cfg = STATUS_CFG[layer.status] ?? STATUS_CFG.YELLOW;
  const Icon = LAYER_ICONS[layer.name] ?? Activity;
  const DirCfg = DIRECTION_CFG[layer.direction] ?? DIRECTION_CFG.NEUTRAL;

  return (
    <div className={`rounded-lg border p-3 space-y-2 ${cfg.cls}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon size={13} />
          <span className="text-[11px] uppercase tracking-widest font-bold">{layer.name}</span>
          <span className="text-[9px] opacity-60">w={layer.weight}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <DirCfg.Icon size={11} className={DirCfg.cls} />
          <span className="text-base font-mono font-bold">{layer.score}<span className="text-[10px] opacity-50">/100</span></span>
          <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
        </div>
      </div>
      <ul className="space-y-0.5">
        {(layer.reasons || []).slice(0, 3).map((r, i) => (
          <li key={i} className="text-[10px] opacity-90 leading-tight">- {r}</li>
        ))}
      </ul>
    </div>
  );
}

function TradePlanRow({ label, value, color = 'text-gray-200' }) {
  return (
    <div className="flex flex-col items-center justify-center bg-[#0a0f17] rounded p-2 border border-emerald-700/30">
      <span className="text-[9px] text-emerald-300/60 uppercase tracking-wider">{label}</span>
      <span className={`font-mono text-xs font-bold ${color}`}>{value ?? '—'}</span>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function HighProbabilityPanel() {
  const [pred, setPred] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [lastAt, setLastAt] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getHighProbabilityPrediction();
      setPred(data);
      setLastAt(new Date());
    } catch (e) {
      setError(e.message ?? 'Prediction failed');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(refresh, POLL_MS);

  if (!pred && !loading && !error) {
    return null;
  }

  const band = pred?.band ?? 'WEAK';
  const bandCfg = BAND_CFG[band] ?? BAND_CFG.WEAK;
  const DirCfg = DIRECTION_CFG[pred?.direction] ?? DIRECTION_CFG.WAIT;
  const probability = pred?.probability ?? 0;

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl overflow-hidden">

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#263044] bg-[#131c27]">
        <div className="flex items-center gap-2.5">
          <Brain size={15} className="text-purple-400" />
          <div>
            <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
              High-Probability Predictor
            </h2>
            <p className="text-[10px] text-slate-500">
              5-layer confluence · Technical + Fundamental + News + Volatility + Sentiment · Decision-only
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {lastAt && (
            <span className="text-[10px] text-gray-600 font-mono">
              {formatKenyaTime(lastAt)} {KENYA_LABEL}
            </span>
          )}
          <button onClick={refresh} disabled={loading} className="text-gray-500 hover:text-gray-300">
            <RefreshCw size={12} className={loading ? 'animate-spin text-purple-400' : ''} />
          </button>
        </div>
      </div>

      <div className="p-5 space-y-4">

        {error && (
          <div className="flex items-center gap-2 text-xs text-red-400 bg-red-900/20 border border-red-800/40 rounded-lg px-3 py-2">
            <AlertTriangle size={13} /> {error}
          </div>
        )}

        {/* Headline: probability + direction + decision */}
        {pred && (
          <div className={`bg-gradient-to-r ${bandCfg.cls} border rounded-xl p-4 space-y-3`}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <DirCfg.Icon size={20} className={DirCfg.cls} />
                <div>
                  <div className="text-2xl font-black tracking-widest">{pred.direction}</div>
                  <div className="text-[10px] uppercase tracking-wider opacity-80">{bandCfg.label}</div>
                </div>
              </div>
              <div className="text-right">
                <div className="text-4xl font-black font-mono leading-none">{probability}<span className="text-base opacity-50">%</span></div>
                <div className="text-[10px] uppercase tracking-wider opacity-80">probability</div>
              </div>
            </div>

            {/* Aligned layers indicator */}
            <div className="flex items-center justify-between text-[11px]">
              <span className="opacity-80">{pred.alignedCount}/{pred.totalLayers} layers aligned</span>
              <div className="flex gap-1">
                {[...Array(pred.totalLayers)].map((_, i) => (
                  <div key={i} className={`w-3 h-1.5 rounded-full ${i < pred.alignedCount ? 'bg-current' : 'bg-current/20'}`} />
                ))}
              </div>
            </div>

            {/* Decision text */}
            <p className="text-[12px] leading-relaxed font-medium border-t border-current/20 pt-2">
              {pred.decision}
            </p>

            {/* Trade plan box (only if technical engine produced one) */}
            {pred.tradePlan && pred.direction !== 'WAIT' && band !== 'AVOID' && (
              <div className="mt-3 pt-3 border-t border-current/20">
                <div className="text-[10px] opacity-70 uppercase tracking-widest mb-2 font-bold">
                  Suggested Trade Plan (manual execution)
                </div>
                <div className="grid grid-cols-4 gap-1.5">
                  <TradePlanRow label="Entry"
                    value={pred.tradePlan.entry?.toFixed(2)}
                    color={pred.direction === 'BUY' ? 'text-emerald-300' : 'text-red-300'} />
                  <TradePlanRow label="Stop Loss"
                    value={pred.tradePlan.stopLoss?.toFixed(2)}
                    color="text-red-300" />
                  <TradePlanRow label="Take Profit"
                    value={pred.tradePlan.takeProfit?.toFixed(2)}
                    color="text-emerald-300" />
                  <TradePlanRow label="R:R"
                    value={pred.tradePlan.rr ? `1:${pred.tradePlan.rr.toFixed(2)}` : '—'}
                    color="text-blue-300" />
                  <TradePlanRow label="Risk pts"
                    value={pred.tradePlan.riskPoints}
                    color="text-amber-300" />
                  <TradePlanRow label="Target pts"
                    value={pred.tradePlan.targetPoints}
                    color="text-emerald-300" />
                  <TradePlanRow label="Score"
                    value={pred.tradePlan.qualityScore != null ? `${pred.tradePlan.qualityScore}/100` : '—'}
                    color="text-purple-300" />
                  <TradePlanRow label="Session" value={pred.tradePlan.session} />
                </div>
                <p className="text-[9px] opacity-60 italic mt-2">
                  No automatic execution. Review all 5 layers above before placing the trade in your broker.
                </p>
              </div>
            )}

            {/* Warnings */}
            {pred.warnings?.length > 0 && (
              <div className="mt-2 pt-2 border-t border-current/20">
                <div className="text-[10px] opacity-70 uppercase tracking-wider mb-1">Warnings</div>
                {pred.warnings.slice(0, 3).map((w, i) => (
                  <p key={i} className="text-[10px] opacity-80 leading-tight">- {w}</p>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 5 layer cards */}
        {pred && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {pred.layers.map(layer => (
              <LayerCard key={layer.name} layer={layer} />
            ))}
          </div>
        )}

        {/* Safety footer */}
        <div className="flex items-center gap-2 text-[9px] text-slate-600 bg-slate-900/40 rounded-lg px-3 py-2 border border-slate-800">
          <Brain size={9} />
          Decision support only. The system does not place trades. You review the layers and decide.
        </div>
      </div>
    </section>
  );
}
