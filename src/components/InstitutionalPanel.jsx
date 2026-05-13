import { Building2, Layers, Zap, BarChart } from 'lucide-react';
import { institutionalData } from '../data/mockData';

function SentimentGauge({ ratio, label }) {
  const sellRatio = 100 - ratio;
  return (
    <div>
      <div className="flex justify-between text-[10px] mb-1">
        <span className="text-emerald-400 font-medium">Buy {ratio}%</span>
        <span className="text-red-400 font-medium">Sell {sellRatio}%</span>
      </div>
      <div className="flex h-2 rounded-full overflow-hidden">
        <div className="bg-emerald-500 transition-all" style={{ width: `${ratio}%` }} />
        <div className="bg-red-500 flex-1" />
      </div>
    </div>
  );
}

function StatRow({ label, value, valueCls = 'text-white' }) {
  return (
    <div className="flex justify-between items-center py-1 border-b border-[#1e2535] last:border-0">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-xs font-mono font-medium ${valueCls}`}>{value}</span>
    </div>
  );
}

export default function InstitutionalPanel() {
  const { cotData, orderFlow, darkPoolActivity, bankLevels } = institutionalData;

  const cotChange = cotData.weeklyChange >= 0 ? `+${cotData.weeklyChange.toLocaleString()}` : cotData.weeklyChange.toLocaleString();
  const flowDelta = orderFlow.delta >= 0 ? `+${(orderFlow.delta / 1e6).toFixed(2)}M` : `${(orderFlow.delta / 1e6).toFixed(2)}M`;

  return (
    <div className="card flex flex-col gap-0">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <Building2 size={13} className="text-purple-400" />
          <span className="card-title">Institutional Flow</span>
        </div>
        <span className="text-[10px] text-gray-600">COT: {cotData.reportDate}</span>
      </div>

      <div className="p-4 space-y-4">
        {/* COT Data */}
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <BarChart size={11} className="text-purple-400" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">CFTC COT Positions</span>
          </div>
          <div className="bg-[#1e2535] rounded-lg p-3 space-y-0">
            <div className="flex items-end gap-2 mb-2">
              <span className={`text-2xl font-black font-mono ${cotData.netPositions >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {cotData.netPositions >= 0 ? '+' : ''}{cotData.netPositions.toLocaleString()}
              </span>
              <span className="text-xs text-gray-500 mb-1">net contracts</span>
            </div>
            <StatRow label="Weekly Change"    value={cotChange}                      valueCls={cotData.weeklyChange >= 0 ? 'text-emerald-400' : 'text-red-400'} />
            <StatRow label="Large Speculators" value={`+${cotData.largeSpreaders.toLocaleString()}`} valueCls="text-emerald-400" />
            <StatRow label="Commercials"      value={cotData.commercials.toLocaleString()} valueCls="text-red-400" />
            <StatRow label="Small Specs"      value={cotData.smallSpeculators.toLocaleString()} valueCls="text-red-400" />
            <div className={`mt-2 inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold ${
              cotData.sentiment === 'Bullish' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-red-500/15 text-red-400'
            }`}>
              {cotData.sentiment} Bias
            </div>
          </div>
        </div>

        {/* Order Flow */}
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <Zap size={11} className="text-yellow-400" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Order Flow</span>
          </div>
          <div className="bg-[#1e2535] rounded-lg p-3 space-y-2">
            <SentimentGauge ratio={orderFlow.buyRatio} />
            <StatRow label="Cumulative Delta" value={flowDelta}            valueCls={orderFlow.delta >= 0 ? 'text-emerald-400' : 'text-red-400'} />
            <StatRow label="Absorption"        value={orderFlow.absorption} valueCls="text-blue-400" />
            <StatRow label="POC"               value={orderFlow.poc.toFixed(5)} />
            <StatRow label="VAH / VAL"         value={`${orderFlow.vah.toFixed(5)} / ${orderFlow.val.toFixed(5)}`} valueCls="text-gray-400" />
          </div>
        </div>

        {/* Dark Pool */}
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <Layers size={11} className="text-blue-400" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Dark Pool Activity</span>
          </div>
          <div className="bg-[#1e2535] rounded-lg p-3">
            <StatRow label="Activity Level"  value={darkPoolActivity.level}                         valueCls="text-amber-400" />
            <StatRow label="Block Prints"    value={`${darkPoolActivity.prints} today`}              />
            <StatRow label="Avg Print Size"  value={`$${(darkPoolActivity.avgSize / 1e6).toFixed(1)}M`} valueCls="text-purple-400" />
            <StatRow label="Trend"           value={darkPoolActivity.trend}                          valueCls="text-emerald-400" />
            <StatRow label="Last Print"      value={darkPoolActivity.lastPrint}                      valueCls="text-gray-400" />
          </div>
        </div>

        {/* Bank Levels */}
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-2">Bank Reference Levels</div>
          <div className="bg-[#1e2535] rounded-lg overflow-hidden">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-[#263044]">
                  <th className="text-left py-1.5 px-3 text-[10px] text-gray-600">Bank</th>
                  <th className="text-right py-1.5 px-3 text-[10px] text-gray-600">Level</th>
                  <th className="text-right py-1.5 px-3 text-[10px] text-gray-600">Type</th>
                </tr>
              </thead>
              <tbody>
                {bankLevels.map((b, i) => (
                  <tr key={i} className="border-b border-[#263044]/50 last:border-0">
                    <td className="py-1.5 px-3 text-gray-400 truncate max-w-[90px]">{b.bank}</td>
                    <td className="py-1.5 px-3 font-mono text-right text-white">{b.level.toFixed(5)}</td>
                    <td className={`py-1.5 px-3 text-right font-medium ${b.type === 'Offer' ? 'text-red-400' : 'text-emerald-400'}`}>
                      {b.type}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
