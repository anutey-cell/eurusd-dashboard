import { useState } from 'react';
import { CheckSquare, Square, ShieldCheck, AlertTriangle, XCircle } from 'lucide-react';
import { checklistCategories } from '../data/mockData';

function CheckItem({ item, checked, onChange }) {
  return (
    <button
      onClick={() => onChange(item.id, !checked)}
      className="flex items-start gap-2.5 w-full text-left group py-1"
    >
      {checked ? (
        <CheckSquare size={15} className="text-emerald-400 flex-shrink-0 mt-0.5" />
      ) : (
        <Square size={15} className="text-gray-600 flex-shrink-0 mt-0.5 group-hover:text-gray-400 transition-colors" />
      )}
      <span className={`text-xs leading-relaxed transition-colors ${
        checked ? 'text-gray-300' : 'text-gray-500 group-hover:text-gray-400'
      }`}>
        {item.label}
        {item.required && <span className="ml-1 text-[9px] text-red-500/60">*</span>}
      </span>
    </button>
  );
}

export default function PreTradeChecklist() {
  const [checks, setChecks] = useState(() => {
    const state = {};
    checklistCategories.forEach(cat =>
      cat.items.forEach(item => { state[item.id] = item.defaultChecked; })
    );
    return state;
  });

  const handleChange = (id, val) => setChecks(prev => ({ ...prev, [id]: val }));

  const allItems = checklistCategories.flatMap(c => c.items);
  const requiredItems = allItems.filter(i => i.required);
  const totalChecked = allItems.filter(i => checks[i.id]).length;
  const requiredChecked = requiredItems.filter(i => checks[i.id]).length;
  const allRequiredDone = requiredChecked === requiredItems.length;
  const progress = Math.round((totalChecked / allItems.length) * 100);

  const gateStatus = allRequiredDone
    ? { icon: ShieldCheck,    cls: 'text-emerald-400', bg: 'bg-emerald-500/10 border-emerald-500/30', label: 'TRADE AUTHORIZED — All required checks passed' }
    : requiredChecked >= requiredItems.length * 0.7
    ? { icon: AlertTriangle,  cls: 'text-amber-400',   bg: 'bg-amber-500/10 border-amber-500/30',    label: `HOLD — ${requiredItems.length - requiredChecked} required check(s) remaining` }
    : { icon: XCircle,        cls: 'text-red-400',     bg: 'bg-red-500/10 border-red-500/30',         label: 'DO NOT TRADE — Critical checks incomplete' };

  const GateIcon = gateStatus.icon;

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Pre-Trade Checklist</span>
        <div className="flex items-center gap-3">
          <span className="text-[10px] text-gray-500">{totalChecked}/{allItems.length} items</span>
          <div className="w-24 h-1.5 bg-[#263044] rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${allRequiredDone ? 'bg-emerald-500' : 'bg-amber-500'}`}
              style={{ width: `${progress}%` }}
            />
          </div>
          <span className="text-[10px] font-mono text-gray-400">{progress}%</span>
        </div>
      </div>

      <div className="p-4">
        {/* Gate Banner */}
        <div className={`flex items-center gap-2.5 border rounded-lg px-3 py-2.5 mb-4 ${gateStatus.bg}`}>
          <GateIcon size={16} className={gateStatus.cls} />
          <span className={`text-xs font-semibold ${gateStatus.cls}`}>{gateStatus.label}</span>
        </div>

        {/* Categories */}
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
          {checklistCategories.map(cat => {
            const catChecked = cat.items.filter(i => checks[i.id]).length;
            const catTotal = cat.items.length;
            const catDone = catChecked === catTotal;
            return (
              <div key={cat.id} className="bg-[#1e2535] rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">{cat.label}</span>
                  <span className={`text-[10px] font-mono ${catDone ? 'text-emerald-400' : 'text-gray-600'}`}>
                    {catChecked}/{catTotal}
                  </span>
                </div>
                <div className="space-y-0.5">
                  {cat.items.map(item => (
                    <CheckItem
                      key={item.id}
                      item={item}
                      checked={!!checks[item.id]}
                      onChange={handleChange}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>

        <p className="text-[10px] text-gray-600 mt-3">
          <span className="text-red-500/60">*</span> Required items. All required checks must be completed before entering a trade.
        </p>
      </div>
    </div>
  );
}
