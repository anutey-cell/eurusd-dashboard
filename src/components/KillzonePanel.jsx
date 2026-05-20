/**
 * KillzonePanel
 *
 * Studies XAU/USD price action across the seven canonical ICT killzones
 * and surfaces:
 *   1. A "current killzone" banner with recommended posture (PRESS/TRADE/OBSERVE/AVOID)
 *   2. A 24-cell hour-of-day edge heatmap (UTC)
 *   3. A per-killzone edge table — win rate, expectancy R, momentum, posture
 *
 * Polls every 60 s. Read-only; never auto-executes a trade.
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import { usePollInterval } from '../hooks/usePollInterval';
import {
  Clock, TrendingUp, TrendingDown, RefreshCw, Sun, Sunrise, Sunset, Moon,
  Layers, Crown, AlertTriangle, ArrowRight,
} from 'lucide-react';
import {
  getCurrentKillzone, getKillzoneEdge, getKillzoneHeatmap,
} from '../services/api';
import {
  utcWindowToKenya, utcHourToKenya, nowKenyaHour, formatKenyaTime, KENYA_LABEL,
} from '../utils/time';

const POLL_MS = 60_000;

const ICONS = { sun: Sun, sunrise: Sunrise, sunset: Sunset, moon: Moon, layers: Layers };

const POSTURE_CFG = {
  PRESS:   { cls: 'bg-emerald-500/20 border-emerald-500/60 text-emerald-300', dot: 'bg-emerald-400' },
  TRADE:   { cls: 'bg-blue-500/20    border-blue-500/60    text-blue-300',    dot: 'bg-blue-400' },
  OBSERVE: { cls: 'bg-amber-500/20   border-amber-500/60   text-amber-300',   dot: 'bg-amber-400' },
  AVOID:   { cls: 'bg-red-500/20     border-red-500/60     text-red-300',     dot: 'bg-red-400' },
};

const BIAS_CFG = {
  BUY_FAVORED:  { cls: 'text-emerald-400', Icon: TrendingUp,   text: 'BUY edge' },
  BUY_ONLY:     { cls: 'text-emerald-400', Icon: TrendingUp,   text: 'BUYs only' },
  SELL_FAVORED: { cls: 'text-red-400',     Icon: TrendingDown, text: 'SELL edge' },
  SELL_ONLY:    { cls: 'text-red-400',     Icon: TrendingDown, text: 'SELLs only' },
  NEUTRAL:      { cls: 'text-gray-400',    Icon: ArrowRight,   text: 'Both sides' },
  INSUFFICIENT: { cls: 'text-gray-500',    Icon: AlertTriangle,text: 'Need more data' },
};

// edge → tailwind bg for heatmap cell
function heatCellCls(score) {
  if (score >= 75) return 'bg-emerald-500/80';
  if (score >= 60) return 'bg-emerald-500/40';
  if (score >= 40) return 'bg-amber-500/40';
  if (score >= 20) return 'bg-red-500/30';
  return 'bg-slate-700/40';
}

// ── Current killzone banner ──────────────────────────────────────────────────

function CurrentBanner({ current }) {
  if (!current) return null;
  const cfg = POSTURE_CFG[current.posture] ?? POSTURE_CFG.OBSERVE;
  const bias = BIAS_CFG[current.directional_bias] ?? BIAS_CFG.INSUFFICIENT;
  const next = current.next_high_edge_kz;
  const eatWindow = utcWindowToKenya(current.window_utc);

  return (
    <div className={`rounded-lg border-2 p-4 ${cfg.cls}`}>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-start gap-3">
          <Clock size={20} className="mt-0.5" />
          <div>
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-widest opacity-80">Active Killzone</span>
              <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot} animate-pulse`} />
            </div>
            <div className="text-2xl font-bold leading-tight">{current.label}</div>
            <div className="text-xs opacity-80 font-mono">
              {eatWindow} {KENYA_LABEL} · {current.minutes_remaining}m remaining
            </div>
          </div>
        </div>

        <div className="text-right">
          <div className="text-[10px] uppercase tracking-widest opacity-80">Edge Score</div>
          <div className="text-4xl font-bold font-mono leading-none">
            {current.edge_score}<span className="text-base opacity-60">/100</span>
          </div>
          <div className="text-[10px] uppercase tracking-widest font-bold mt-1">{current.posture}</div>
        </div>
      </div>

      <div className="text-xs opacity-90 mt-2 pl-8">{current.posture_reason}</div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-4 pl-8">
        <Stat label="Win Rate"     value={`${current.win_rate}%`}    />
        <Stat label="Expectancy"   value={`${current.expectancy_r}R`} />
        <Stat label="Bias" Icon={bias.Icon} value={bias.text} valueCls={bias.cls} />
        <Stat label="Best Setup"   value={current.best_setup || '—'} mono={false} />
      </div>

      {next && (
        <div className="mt-3 pt-3 border-t border-current/20 flex items-center gap-2 text-xs opacity-80">
          <ArrowRight size={12} />
          <span>Next high-edge window:</span>
          <span className="font-bold">{next.label}</span>
          <span className="font-mono">({utcWindowToKenya(next.window_utc)} {KENYA_LABEL})</span>
          <span className="opacity-60">in {next.hours_away}h · edge {next.edge_score}/100</span>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, Icon, valueCls = '', mono = true }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest opacity-70">{label}</div>
      <div className={`flex items-center gap-1.5 text-sm font-bold ${mono ? 'font-mono' : ''} ${valueCls}`}>
        {Icon && <Icon size={12} />}
        {value}
      </div>
    </div>
  );
}

// ── Hour heatmap ─────────────────────────────────────────────────────────────

function HourHeatmap({ cells, currentEatHour }) {
  // Rotate so the strip reads 00:00..23:00 in EAT (each cell's underlying
  // killzone analysis is still keyed by UTC, but we shift the display).
  const eatCells = useMemo(() => {
    if (!cells?.length) return [];
    return Array.from({ length: 24 }, (_, eatHour) => {
      const utcHour = ((eatHour - 3) + 24) % 24;
      const c = cells.find(x => x.hour === utcHour);
      return c ? { ...c, eatHour } : { eatHour, hour: utcHour, edge_score: 0, posture: 'OBSERVE', kz_label: '—' };
    });
  }, [cells]);

  if (!eatCells.length) return null;
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-gray-400 uppercase tracking-widest">
          24h Edge Heatmap · {KENYA_LABEL}
        </span>
        <div className="flex items-center gap-2 text-[10px] text-gray-500">
          <LegendDot cls="bg-emerald-500/80" label="PRESS" />
          <LegendDot cls="bg-emerald-500/40" label="TRADE" />
          <LegendDot cls="bg-amber-500/40"   label="OBSERVE" />
          <LegendDot cls="bg-red-500/30"     label="AVOID" />
        </div>
      </div>
      <div className="grid grid-cols-24 gap-0.5" style={{ gridTemplateColumns: 'repeat(24, minmax(0, 1fr))' }}>
        {eatCells.map(c => (
          <div
            key={c.eatHour}
            className={`relative h-10 rounded ${heatCellCls(c.edge_score)} ${c.eatHour === currentEatHour ? 'ring-2 ring-white' : ''}`}
            title={`${String(c.eatHour).padStart(2,'0')}:00 ${KENYA_LABEL} (${String(c.hour).padStart(2,'0')}:00 UTC) · ${c.kz_label} · edge ${c.edge_score} · ${c.posture}`}
          >
            <span className="absolute inset-x-0 bottom-0 text-[9px] text-center font-mono text-white/90">
              {String(c.eatHour).padStart(2,'0')}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function LegendDot({ cls, label }) {
  return (
    <div className="flex items-center gap-1">
      <span className={`w-2 h-2 rounded ${cls}`} />
      <span>{label}</span>
    </div>
  );
}

// ── Per-killzone table ───────────────────────────────────────────────────────

function KZTable({ killzones, bestKey, worstKey }) {
  if (!killzones?.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-gray-400 uppercase tracking-widest text-[10px] border-b border-[#263044]">
            <th className="text-left py-2 pl-1">Killzone</th>
            <th className="text-center">Window ({KENYA_LABEL})</th>
            <th className="text-right">Edge</th>
            <th className="text-center">Posture</th>
            <th className="text-right">WR</th>
            <th className="text-right">Exp R</th>
            <th className="text-right">n</th>
            <th className="text-center">Bias</th>
            <th className="text-right">Range</th>
            <th className="text-right">Momentum</th>
            <th className="text-left pl-3">Best Setup</th>
          </tr>
        </thead>
        <tbody>
          {killzones.map(kz => {
            const cfg  = POSTURE_CFG[kz.posture] ?? POSTURE_CFG.OBSERVE;
            const bias = BIAS_CFG[kz.observations.directional_bias] ?? BIAS_CFG.INSUFFICIENT;
            const Icon = ICONS[kz.icon] ?? Layers;
            const isBest  = kz.key === bestKey;
            const isWorst = kz.key === worstKey;
            return (
              <tr key={kz.key} className="border-b border-[#1c2333] hover:bg-[#1c2333]/40">
                <td className="py-2 pl-1">
                  <div className="flex items-center gap-2">
                    <Icon size={12} className="text-gray-500" />
                    <span className="font-semibold text-gray-200">{kz.label}</span>
                    {isBest  && <Crown size={11} className="text-emerald-400" title="Best edge" />}
                    {isWorst && <AlertTriangle size={11} className="text-red-400" title="Worst edge" />}
                  </div>
                </td>
                <td className="text-center font-mono text-gray-400">{utcWindowToKenya(kz.window_utc)}</td>
                <td className="text-right font-mono font-bold text-white">{kz.edge_score}</td>
                <td className="text-center">
                  <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold border ${cfg.cls}`}>
                    {kz.posture}
                  </span>
                </td>
                <td className="text-right font-mono">{kz.observations.win_rate}%</td>
                <td className="text-right font-mono">{kz.observations.expectancy_r}</td>
                <td className="text-right font-mono text-gray-500">
                  {kz.observations.resolved}/{kz.observations.count}
                </td>
                <td className="text-center">
                  <span className={`inline-flex items-center gap-1 text-[10px] ${bias.cls}`}>
                    <bias.Icon size={9} />
                    {bias.text}
                  </span>
                </td>
                <td className="text-right font-mono text-gray-400">{kz.price_action.avg_range_pts}</td>
                <td className="text-right font-mono text-gray-400">
                  {(kz.price_action.momentum * 100).toFixed(0)}%
                </td>
                <td className="pl-3 text-gray-400 text-[10px]">{kz.observations.best_setup || '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Main panel ───────────────────────────────────────────────────────────────

export default function KillzonePanel() {
  const [current, setCurrent] = useState(null);
  const [report,  setReport]  = useState(null);
  const [heat,    setHeat]    = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);
  const [lookback,setLookback]= useState(60);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [c, r, h] = await Promise.all([
        getCurrentKillzone({ lookbackDays: lookback }),
        getKillzoneEdge({ lookbackDays: lookback }),
        getKillzoneHeatmap({ lookbackDays: lookback }),
      ]);
      setCurrent(c);
      setReport(r);
      setHeat(h?.cells || []);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Killzone analyzer unavailable');
    } finally {
      setLoading(false);
    }
  }, [lookback]);

  // Visibility-aware polling (pauses when tab is hidden)
  usePollInterval(() => load(true), POLL_MS);
  // First load runs once on mount (full spinner, not silent)
  useEffect(() => { load(false); /* eslint-disable-line */ }, []);

  const currentEatHour = nowKenyaHour();

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Layers size={16} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">Killzone Edge Analyzer</h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            Per-session price action + paper-observation edge ranking
          </span>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <label className="text-gray-500">Lookback:</label>
          <select
            className="bg-[#161b27] border border-[#263044] rounded px-2 py-0.5 text-gray-200 text-xs"
            value={lookback}
            onChange={e => setLookback(Number(e.target.value))}
          >
            <option value={14}>14 d</option>
            <option value={30}>30 d</option>
            <option value={60}>60 d</option>
            <option value={90}>90 d</option>
            <option value={180}>180 d</option>
          </select>
          <button
            onClick={() => load(false)}
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

      {!error && current && <CurrentBanner current={current} />}
      {!error && heat?.length > 0 && <HourHeatmap cells={heat} currentEatHour={currentEatHour} />}
      {!error && report?.killzones && (
        <KZTable
          killzones={report.killzones}
          bestKey={report.best_kz}
          worstKey={report.worst_kz}
        />
      )}

      {report?.generated_at && (
        <div className="text-[10px] text-gray-600 text-right">
          Updated {formatKenyaTime(report.generated_at)} {KENYA_LABEL} · lookback {lookback}d
        </div>
      )}
    </div>
  );
}
