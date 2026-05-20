/**
 * TraderDevelopmentPanel
 *
 * Renders the 20-principle curriculum sourced from canonical trading-psychology
 * books (Mark Douglas, Van Tharp, Steenbarger, Schwager, Kahneman, Taleb,
 * Elder, Murphy, Covel, Peterson) with the engine's self-evaluation per
 * principle: FULL / PARTIAL / MISSING.
 *
 * Designed as an evolving curriculum — as the engine accumulates trades,
 * implements features, and survives drawdowns, principles graduate from
 * MISSING → PARTIAL → FULL. The panel surfaces "next 3 to internalize"
 * as the path forward.
 */
import { useState, useCallback } from 'react';
import {
  BookOpen, CheckCircle, Circle, AlertTriangle, RefreshCw, Award,
  Brain, Shield, Activity, Layers, Compass, Zap,
} from 'lucide-react';
import { getTraderDevelopment } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 10 * 60_000;   // 10 min — moves slowly

const CATEGORY_ICONS = {
  mindset:    Brain,
  risk:       Shield,
  discipline: CheckCircle,
  edge:       Award,
  structure:  Layers,
  cognitive:  Brain,
  execution:  Zap,
  learning:   BookOpen,
};

const STATUS_CFG = {
  FULL:    { cls: 'border-emerald-500/50 bg-emerald-500/10', icon: CheckCircle,    iconCls: 'text-emerald-400', label: 'EMBODIED' },
  PARTIAL: { cls: 'border-amber-500/50   bg-amber-500/10',   icon: Circle,          iconCls: 'text-amber-400',   label: 'PARTIAL' },
  MISSING: { cls: 'border-red-500/40     bg-red-500/5',      icon: AlertTriangle,   iconCls: 'text-red-400',     label: 'MISSING' },
};

function PrincipleCard({ p, compact = false }) {
  const cfg = STATUS_CFG[p.status] ?? STATUS_CFG.PARTIAL;
  const CatIcon = CATEGORY_ICONS[p.category] ?? Brain;
  const StatusIcon = cfg.icon;
  return (
    <div className={`rounded border p-3 space-y-2 ${cfg.cls}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2 flex-1 min-w-0">
          <CatIcon size={13} className="text-gray-400 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="text-[11px] uppercase tracking-widest font-bold text-gray-100 leading-tight">
              {p.name}
            </div>
            <div className="text-[9px] text-gray-500 italic mt-0.5">
              {p.book} — {p.author}
            </div>
          </div>
        </div>
        <div className={`flex items-center gap-1 ${cfg.iconCls}`}>
          <StatusIcon size={12} />
          <span className="text-[9px] font-bold uppercase tracking-widest">{cfg.label}</span>
        </div>
      </div>
      {!compact && (
        <>
          <div className="text-[10px] text-gray-300 leading-snug italic">
            "{p.text}"
          </div>
          <div className="text-[10px] text-gray-400 leading-snug pl-2 border-l-2 border-current/30">
            <span className="text-gray-500 mr-1">Engine implication:</span>
            {p.engine_implication}
          </div>
        </>
      )}
    </div>
  );
}

export default function TraderDevelopmentPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(true);

  const load = useCallback(async () => {
    try {
      const d = await getTraderDevelopment();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Trader-development data unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(load, POLL_MS);

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <BookOpen size={16} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Trader Development Curriculum
          </h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            principles from Douglas · Tharp · Steenbarger · Schwager · Kahneman · Taleb · Elder · Covel
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => setExpanded(v => !v)}
            className="text-[10px] text-gray-500 hover:text-gray-300 px-2 py-1 rounded border border-[#263044]">
            {expanded ? 'compact' : 'expand'}
          </button>
          <button onClick={load} disabled={loading}
            className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-300">
            <RefreshCw size={10} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded p-2 text-xs text-red-300 flex items-center gap-2">
          <AlertTriangle size={11} />
          {error}
        </div>
      )}

      {data && (
        <>
          {/* Progress banner */}
          <div className="rounded border-2 border-blue-500/40 bg-blue-500/5 p-3">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div>
                <div className="text-[10px] uppercase tracking-widest text-blue-300 font-bold">
                  Internalization Progress
                </div>
                <div className="text-2xl font-mono font-bold text-white mt-0.5">
                  {data.percent_internalized}%
                  <span className="text-xs text-gray-500 ml-2 font-sans">
                    of {data.total_principles} principles
                  </span>
                </div>
                <div className="text-[10px] text-gray-400 mt-1">
                  <span className="text-emerald-400">{data.full}</span> embodied ·
                  <span className="text-amber-400 ml-1">{data.partial}</span> partial ·
                  <span className="text-red-400 ml-1">{data.missing}</span> missing
                </div>
              </div>
              <div className="flex-1 max-w-md">
                <div className="h-2 bg-slate-800 rounded overflow-hidden flex">
                  <div className="bg-emerald-500" style={{width: `${(data.full / data.total_principles) * 100}%`}} />
                  <div className="bg-amber-500"  style={{width: `${(data.partial / data.total_principles) * 100}%`}} />
                  <div className="bg-red-500/50" style={{width: `${(data.missing / data.total_principles) * 100}%`}} />
                </div>
                <div className="text-[9px] text-gray-500 mt-1">
                  Library: {data.books_referenced?.length} canonical books referenced
                </div>
              </div>
            </div>
          </div>

          {/* Next 3 to internalise */}
          {data.next_to_internalize?.length > 0 && (
            <div className="rounded border border-amber-500/40 bg-amber-500/5 p-3 space-y-2">
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-amber-300 font-bold">
                <Compass size={11} />
                Next 3 Principles To Internalize
              </div>
              {data.next_to_internalize.map(p => (
                <div key={p.id} className="text-xs">
                  <span className="font-bold text-amber-200">{p.name}</span>
                  <span className="text-amber-300/70 text-[10px] ml-2 italic">
                    — {p.book} ({p.author})
                  </span>
                  <div className="text-[10px] text-amber-200/80 mt-0.5 pl-2">
                    {p.engine_implication}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* All principles, grouped by category */}
          <div className="space-y-3">
            {Object.entries(data.by_category || {}).map(([cat, items]) => (
              <div key={cat}>
                <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-2 flex items-center gap-1">
                  {(() => {
                    const Icon = CATEGORY_ICONS[cat] ?? Brain;
                    return <Icon size={11} />;
                  })()}
                  {cat} ({items.length})
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {items.map(p => <PrincipleCard key={p.id} p={p} compact={!expanded} />)}
                </div>
              </div>
            ))}
          </div>

          <div className="text-[10px] text-gray-600 text-right">
            Updated {formatKenyaTime(data.generated_at)} {KENYA_LABEL} · {data.books_referenced?.length || 0} books
          </div>
        </>
      )}
    </div>
  );
}
