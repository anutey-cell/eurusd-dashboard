/**
 * TraderMindsetPanel
 *
 * Renders the 10-dimension trader-mindset scorecard from
 * /api/v1/diagnostics/trader-mindset. Answers the question:
 *   "Does this engine have the mindset of a profitable trader yet?"
 *
 * Visual layout:
 *   - Top: big band badge (PRO_MINDSET / APPRENTICE / BEGINNER / PRE_TRADER)
 *     + total score X/100 + headline
 *   - 10 dimension cards in a 2-col grid, each with score-bar + bullets
 *   - Bottom: prioritised recommendations
 */
import { useState, useEffect, useCallback } from 'react';
import { usePollInterval } from '../hooks/usePollInterval';
import {
  Award, AlertTriangle, CheckCircle, RefreshCw, Trophy, Target, Shield,
  Brain, Activity, BookOpen, Layers, Wifi, Clock, GraduationCap,
} from 'lucide-react';
import { getTraderMindset } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';

const POLL_MS = 5 * 60_000;   // 5 min — moves slowly

const BAND_CFG = {
  PRO_MINDSET: { cls: 'border-emerald-500/60 bg-emerald-500/15 text-emerald-300', Icon: Trophy,         label: 'PRO MINDSET' },
  APPRENTICE:  { cls: 'border-blue-500/60    bg-blue-500/15    text-blue-300',    Icon: GraduationCap,  label: 'APPRENTICE'  },
  BEGINNER:    { cls: 'border-amber-500/60   bg-amber-500/15   text-amber-300',   Icon: Target,         label: 'BEGINNER'    },
  PRE_TRADER:  { cls: 'border-red-500/60     bg-red-500/15     text-red-300',     Icon: AlertTriangle,  label: 'PRE-TRADER'  },
};

const DIM_ICON = {
  'EDGE':           Trophy,
  'RISK':           Shield,
  'DISCIPLINE':     CheckCircle,
  'SAMPLE SIZE':    Activity,
  'DRAWDOWN':       AlertTriangle,
  'ADAPTATION':     Brain,
  'DIVERSITY':      Layers,
  'INFRASTRUCTURE': Wifi,
  'JOURNALING':     BookOpen,
  'PATIENCE':       Clock,
};

function ScoreBar({ score, max }) {
  const pct = Math.round((score / max) * 100);
  const cls = pct >= 80 ? 'bg-emerald-500'
           : pct >= 60 ? 'bg-blue-500'
           : pct >= 40 ? 'bg-amber-500'
           :             'bg-red-500';
  return (
    <div className="h-1.5 bg-slate-800 rounded overflow-hidden">
      <div className={`h-full ${cls} transition-all`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function DimCard({ d }) {
  const Icon = DIM_ICON[d.dimension] || Activity;
  const pct = Math.round((d.score / d.max) * 100);
  const verdictCls = pct >= 80 ? 'text-emerald-300'
                  : pct >= 60 ? 'text-blue-300'
                  : pct >= 40 ? 'text-amber-300'
                  :             'text-red-300';
  return (
    <div className="rounded border border-[#263044] bg-[#161b27] p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon size={12} className="text-gray-400" />
          <span className="text-[10px] uppercase tracking-widest font-bold text-gray-200">
            {d.dimension}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-bold uppercase ${verdictCls}`}>{d.verdict}</span>
          <span className="text-base font-mono font-bold text-white">
            {d.score}<span className="text-[10px] text-gray-500">/{d.max}</span>
          </span>
        </div>
      </div>
      <ScoreBar score={d.score} max={d.max} />
      <ul className="space-y-0.5">
        {(d.bullets || []).map((b, i) => (
          <li key={i} className="text-[10px] text-gray-400 leading-tight">- {b}</li>
        ))}
      </ul>
    </div>
  );
}

export default function TraderMindsetPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await getTraderMindset();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Mindset scorecard unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(load, POLL_MS);

  const bandCfg = data?.band ? (BAND_CFG[data.band] || BAND_CFG.PRE_TRADER) : null;

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Award size={16} className="text-amber-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Trader Mindset Scorecard
          </h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            10 dimensions of profitable-trader DNA
          </span>
        </div>
        <button onClick={load} disabled={loading}
          className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-300">
          <RefreshCw size={10} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded p-2 text-xs text-red-300 flex items-center gap-2">
          <AlertTriangle size={11} />
          {error}
        </div>
      )}

      {data && (
        <>
          {/* Top banner: band + total */}
          <div className={`rounded border-2 p-4 ${bandCfg.cls}`}>
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div className="flex items-center gap-3">
                <bandCfg.Icon size={28} />
                <div>
                  <div className="text-[10px] uppercase tracking-widest opacity-80">
                    Current band
                  </div>
                  <div className="text-2xl font-bold leading-tight">{bandCfg.label}</div>
                  <div className="text-xs opacity-90 mt-1">{data.headline}</div>
                </div>
              </div>
              <div className="text-right">
                <div className="text-[10px] uppercase tracking-widest opacity-70">Total Score</div>
                <div className="text-5xl font-mono font-bold leading-none">
                  {data.total}<span className="text-xl opacity-50">/{data.max}</span>
                </div>
                <div className="text-xs font-mono opacity-90">{data.percent}%</div>
              </div>
            </div>
          </div>

          {/* 10 dimension cards in 2-col grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {data.dimensions.map(d => <DimCard key={d.dimension} d={d} />)}
          </div>

          {/* Recommendations */}
          {data.recommendations?.length > 0 && (
            <div className="rounded border border-amber-500/40 bg-amber-500/5 p-3 space-y-1.5">
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-amber-300 font-bold">
                <AlertTriangle size={11} />
                Path to Pro Mindset
              </div>
              {data.recommendations.map((r, i) => (
                <div key={i} className="text-xs text-amber-200 leading-snug">
                  <span className="text-amber-400 mr-1">•</span>{r}
                </div>
              ))}
            </div>
          )}

          <div className="text-[10px] text-gray-600 text-right">
            Updated {formatKenyaTime(data.generated_at)} {KENYA_LABEL}
          </div>
        </>
      )}
    </div>
  );
}
