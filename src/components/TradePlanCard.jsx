import { useState, useEffect } from 'react';
import { Target, ShieldAlert, Clock, StickyNote, DollarSign } from 'lucide-react';
import { tradePlan as mockTradePlan } from '../data/mockData';
import { SkeletonCard } from './Skeleton';
import { FallbackChip } from './BackendBanner';

function useCountdown(targetIso) {
  const [remaining, setRemaining] = useState('');
  useEffect(() => {
    const tick = () => {
      const diff = new Date(targetIso) - Date.now();
      if (diff <= 0) { setRemaining('Expired'); return; }
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      setRemaining(`${h}h ${m}m ${s}s`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [targetIso]);
  return remaining;
}

function PriceRow({ label, price, pips, badge, badgeCls, pipsCls }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-[#1e2535] last:border-0">
      <div className="flex items-center gap-2">
        {badge && <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded font-mono ${badgeCls}`}>{badge}</span>}
        <span className="text-xs text-gray-400">{label}</span>
      </div>
      <div className="flex items-center gap-3">
        {pips !== undefined && (
          <span className={`text-xs font-mono ${pipsCls}`}>{pips > 0 ? '+' : ''}{pips} pips</span>
        )}
        <span className="font-mono text-sm font-semibold text-white">{price.toFixed(5)}</span>
      </div>
    </div>
  );
}

// Props supplied by Dashboard; mock default makes component work standalone.
export default function TradePlanCard({
  loading         = false,
  error           = null,
  isUsingFallback = false,
  tradePlan:      p = mockTradePlan,
}) {
  const countdown = useCountdown(p?.validity ?? mockTradePlan.validity);

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <span className="card-title">Trade Plan</span>
          <FallbackChip isUsingFallback={isUsingFallback} error={error} />
        </div>
        <div className="flex items-center gap-1 text-[10px] text-amber-400">
          <Clock size={10} />
          <span className="font-mono">{countdown}</span>
        </div>
      </div>

      {loading ? (
        <SkeletonCard rows={8} />
      ) : (
        <div className="p-4 space-y-4">
          {/* Entry / SL / TPs */}
          <div className="bg-[#1e2535] rounded-lg p-3">
            <PriceRow
              label="Entry" price={p.entry}
              badge="ENTRY" badgeCls="bg-blue-500/20 text-blue-400 border border-blue-500/30"
            />
            <PriceRow
              label="Stop Loss" price={p.stopLoss} pips={-p.stopLossPips}
              badge="SL" badgeCls="bg-red-500/20 text-red-400 border border-red-500/30"
              pipsCls="text-red-400"
            />
            {p.targets.map(t => (
              <PriceRow
                key={t.label}
                label={`${t.partial} close`} price={t.price} pips={t.pips}
                badge={t.label} badgeCls="bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                pipsCls="text-emerald-400"
              />
            ))}
          </div>

          {/* Stats grid */}
          <div className="grid grid-cols-2 gap-2">
            {[
              { label: 'Risk %',        value: `${p.riskPercent}%`,              icon: ShieldAlert, cls: 'text-amber-400' },
              { label: 'Risk Amount',   value: `$${p.riskAmount}`,               icon: DollarSign,  cls: 'text-amber-400' },
              { label: 'Position Size', value: `${p.positionSize} lots`,         icon: Target,      cls: 'text-blue-400'  },
              { label: 'Account',       value: `$${p.accountSize.toLocaleString()}`, icon: DollarSign, cls: 'text-gray-400' },
            ].map(item => {
              const Icon = item.icon;
              return (
                <div key={item.label} className="bg-[#1e2535] rounded-lg p-2.5 flex items-center gap-2">
                  <Icon size={13} className={item.cls} />
                  <div>
                    <div className="text-[10px] text-gray-500">{item.label}</div>
                    <div className="font-mono text-sm font-semibold text-white">{item.value}</div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* R:R chips */}
          <div>
            <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">Risk : Reward</div>
            <div className="flex gap-2">
              {p.targets.map(t => (
                <div key={t.label} className="flex-1 bg-[#1e2535] rounded-lg p-2 text-center">
                  <div className="text-[10px] text-gray-500">{t.label}</div>
                  <div className="font-mono text-sm font-bold text-emerald-400">1:{t.rr}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Notes */}
          <div className="bg-[#1e2535] rounded-lg p-3">
            <div className="flex items-center gap-1.5 mb-1.5">
              <StickyNote size={11} className="text-gray-500" />
              <span className="text-[10px] text-gray-500 uppercase tracking-wider">Trade Notes</span>
            </div>
            <p className="text-xs text-gray-400 leading-relaxed">{p.notes}</p>
          </div>
        </div>
      )}
    </div>
  );
}
