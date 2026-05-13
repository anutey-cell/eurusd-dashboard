import { ArrowUpCircle, ArrowDownCircle, MinusCircle } from 'lucide-react';
import { currentSignal as mockSignal } from '../data/mockData';
import { SkeletonCard } from './Skeleton';
import { FallbackChip } from './BackendBanner';

const DIRECTION_CONFIG = {
  BUY:     { icon: ArrowUpCircle,   cls: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/30', label: 'LONG / BUY'   },
  SELL:    { icon: ArrowDownCircle, cls: 'text-red-400',     bg: 'bg-red-500/10',     border: 'border-red-500/30',     label: 'SHORT / SELL'  },
  NEUTRAL: { icon: MinusCircle,     cls: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/30',  label: 'NO SIGNAL'    },
};

function ScoreBar({ score }) {
  const color = score >= 75 ? 'bg-emerald-500' : score >= 55 ? 'bg-blue-500' : 'bg-red-500';
  return (
    <div className="w-24 h-1.5 bg-[#263044] rounded-full overflow-hidden">
      <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${score}%` }} />
    </div>
  );
}

// Props supplied by Dashboard; mock defaults make the component work standalone too.
export default function SignalCard({
  loading         = false,
  error           = null,
  isUsingFallback = false,
  currentSignal:  s = mockSignal,
}) {
  const cfg      = DIRECTION_CONFIG[s?.direction] ?? DIRECTION_CONFIG.NEUTRAL;
  const Icon     = cfg.icon;
  const timeStr  = s ? new Date(s.timestamp).toUTCString().slice(5, 22) + ' UTC' : '';
  const avgScore = s ? Math.round(s.factors.reduce((a, f) => a + f.score, 0) / s.factors.length) : 0;

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <span className="card-title">Signal</span>
          <FallbackChip isUsingFallback={isUsingFallback} error={error} />
        </div>
        <span className="text-[10px] text-gray-600 font-mono">
          {s?.timeframe} • {s?.session} Session
        </span>
      </div>

      {loading ? (
        <SkeletonCard rows={6} />
      ) : (
        <>
          {/* Direction hero */}
          <div className={`mx-4 mt-4 rounded-xl border ${cfg.border} ${cfg.bg} p-4 flex items-center justify-between`}>
            <div className="flex items-center gap-3">
              <Icon size={36} className={cfg.cls} />
              <div>
                <div className={`text-2xl font-black tracking-wider ${cfg.cls}`}>{cfg.label}</div>
                <div className="text-xs text-gray-500 mt-0.5">EUR/USD • {timeStr}</div>
              </div>
            </div>
            <div className="text-right">
              <div className={`text-3xl font-black font-mono ${cfg.cls}`}>{s?.confidence}%</div>
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">Confidence</div>
            </div>
          </div>

          {/* Strength meters */}
          <div className="grid grid-cols-2 gap-3 mx-4 mt-3">
            {[
              { label: 'Signal Strength',  value: s?.strength,  grad: 'from-blue-600 to-blue-400'       },
              { label: 'Avg Factor Score', value: avgScore,      grad: 'from-emerald-600 to-emerald-400' },
            ].map(({ label, value, grad }) => (
              <div key={label} className="bg-[#1e2535] rounded-lg p-3">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">{label}</div>
                <div className="flex items-end gap-1 mb-2">
                  <span className="text-xl font-bold font-mono text-white">{value}</span>
                  <span className="text-gray-500 text-xs mb-0.5">/100</span>
                </div>
                <div className="w-full h-2 bg-[#263044] rounded-full overflow-hidden">
                  <div className={`h-full rounded-full bg-gradient-to-r ${grad}`} style={{ width: `${value}%` }} />
                </div>
              </div>
            ))}
          </div>

          {/* Factors */}
          <div className="mx-4 mt-3 mb-4 space-y-1.5">
            <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">Factor Breakdown</div>
            {s?.factors.map(f => (
              <div key={f.name} className="flex items-center justify-between gap-2">
                <span className="text-xs text-gray-400 truncate flex-1">{f.name}</span>
                <span className="text-[10px] text-gray-600 truncate max-w-[110px] text-right">{f.value}</span>
                <div className="flex items-center gap-1.5">
                  <ScoreBar score={f.score} />
                  <span className="text-[10px] font-mono text-gray-500 w-6 text-right">{f.score}</span>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
