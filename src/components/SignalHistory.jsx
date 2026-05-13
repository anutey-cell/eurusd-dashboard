import { useState } from 'react';
import { History, ChevronUp, ChevronDown, TrendingUp, TrendingDown, RefreshCw } from 'lucide-react';
import { signalHistory as mockHistory } from '../data/mockData';
import { SkeletonTable } from './Skeleton';
import { FallbackChip } from './BackendBanner';

const RESULT_CONFIG = {
  WIN:       { bg: 'bg-emerald-500/10 text-emerald-400' },
  LOSS:      { bg: 'bg-red-500/10 text-red-400'         },
  BREAKEVEN: { bg: 'bg-gray-500/10 text-gray-400'       },
  PENDING:   { bg: 'bg-amber-500/10 text-amber-400'     },
};

const COLS = [
  { key: 'date',       label: 'Date',   cls: 'text-left'  },
  { key: 'dir',        label: 'Dir',    cls: 'text-left'  },
  { key: 'entry',      label: 'Entry',  cls: 'text-right' },
  { key: 'exit',       label: 'Exit',   cls: 'text-right' },
  { key: 'pips',       label: 'Pips',   cls: 'text-right' },
  { key: 'rr',         label: 'R:R',    cls: 'text-right' },
  { key: 'pnl',        label: 'P&L',    cls: 'text-right' },
  { key: 'confidence', label: 'Conf%',  cls: 'text-right' },
  { key: 'result',     label: 'Result', cls: 'text-center'},
];

function SortIcon({ col, sortBy, dir }) {
  if (sortBy !== col) return <ChevronUp size={10} className="text-gray-700" />;
  return dir === 'asc' ? <ChevronUp size={10} className="text-blue-400" /> : <ChevronDown size={10} className="text-blue-400" />;
}

// Props supplied by Dashboard; mock default makes component work standalone.
export default function SignalHistory({
  loading         = false,
  error           = null,
  isUsingFallback = false,
  refetch         = null,
  trades          = mockHistory,
}) {
  const [sortBy,  setSortBy]  = useState('id');
  const [sortDir, setSortDir] = useState('desc');
  const [filter,  setFilter]  = useState('ALL');

  const allTrades = trades ?? mockHistory;

  const handleSort = col => {
    if (sortBy === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortBy(col); setSortDir('desc'); }
  };

  const filtered = allTrades
    .filter(t => filter === 'ALL' || t.result === filter)
    .sort((a, b) => {
      let av = a[sortBy], bv = b[sortBy];
      if (typeof av === 'string') { av = av.toLowerCase(); bv = bv.toLowerCase(); }
      return av < bv ? (sortDir === 'asc' ? -1 : 1) : av > bv ? (sortDir === 'asc' ? 1 : -1) : 0;
    });

  const wins     = allTrades.filter(t => t.result === 'WIN').length;
  const losses   = allTrades.filter(t => t.result === 'LOSS').length;
  const totalPnl = allTrades.reduce((a, t) => a + t.pnl, 0);

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <History size={13} className="text-blue-400" />
          <span className="card-title">Signal History</span>
          <span className="text-[10px] text-gray-600">({allTrades.length} trades)</span>
          <FallbackChip isUsingFallback={isUsingFallback} error={error} />
        </div>
        <div className="flex items-center gap-2">
          {refetch && (
            <button onClick={refetch} title="Refresh" className="text-gray-600 hover:text-gray-400 transition-colors">
              <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
            </button>
          )}
          <div className="hidden md:flex items-center gap-3 mr-1">
            <span className="text-[10px] text-emerald-400 font-mono">{wins}W</span>
            <span className="text-[10px] text-red-400 font-mono">{losses}L</span>
            <span className={`text-[10px] font-mono font-semibold ${totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {totalPnl >= 0 ? '+' : ''}${totalPnl.toLocaleString()}
            </span>
          </div>
          <div className="flex bg-[#0d1117] rounded-lg border border-[#263044] p-0.5 gap-0.5">
            {['ALL', 'WIN', 'LOSS'].map(f => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-2 py-0.5 text-[10px] font-medium rounded-md transition-colors ${
                  filter === f
                    ? f === 'WIN'  ? 'bg-emerald-600 text-white'
                    : f === 'LOSS' ? 'bg-red-700 text-white'
                    : 'bg-[#263044] text-white'
                    : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
      </div>

      {loading ? (
        <SkeletonTable cols={9} rows={6} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs min-w-[640px]">
            <thead>
              <tr className="border-b border-[#263044]">
                {COLS.map(col => (
                  <th
                    key={col.key}
                    onClick={() => handleSort(col.key)}
                    className={`py-2.5 px-3 cursor-pointer select-none text-[10px] font-semibold uppercase tracking-wider text-gray-600 hover:text-gray-400 transition-colors ${col.cls}`}
                  >
                    <span className="inline-flex items-center gap-1">
                      {col.label}
                      <SortIcon col={col.key} sortBy={sortBy} dir={sortDir} />
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((t, i) => {
                const rc = RESULT_CONFIG[t.result] ?? RESULT_CONFIG.PENDING;
                return (
                  <tr
                    key={t.id}
                    className={`border-b border-[#263044]/40 hover:bg-[#1e2535]/50 transition-colors ${i % 2 ? 'bg-[#161b27]/30' : ''}`}
                  >
                    <td className="py-2 px-3 text-gray-500 font-mono whitespace-nowrap">
                      {t.date} <span className="text-gray-700">{t.time}</span>
                    </td>
                    <td className="py-2 px-3">
                      <span className={`flex items-center gap-1 font-semibold ${t.dir === 'BUY' ? 'text-emerald-400' : 'text-red-400'}`}>
                        {t.dir === 'BUY' ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
                        {t.dir}
                      </span>
                    </td>
                    <td className="py-2 px-3 font-mono text-right text-gray-300">{t.entry.toFixed(5)}</td>
                    <td className="py-2 px-3 font-mono text-right text-gray-300">{t.exit.toFixed(5)}</td>
                    <td className={`py-2 px-3 font-mono text-right font-semibold ${t.pips > 0 ? 'text-emerald-400' : t.pips < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                      {t.pips > 0 ? '+' : ''}{t.pips}
                    </td>
                    <td className={`py-2 px-3 font-mono text-right ${t.rr > 0 ? 'text-blue-400' : 'text-red-400'}`}>
                      {t.rr > 0 ? `1:${t.rr}` : t.rr}
                    </td>
                    <td className={`py-2 px-3 font-mono text-right font-semibold ${t.pnl > 0 ? 'text-emerald-400' : t.pnl < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                      {t.pnl > 0 ? '+' : ''}${t.pnl}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-gray-500">{t.confidence}%</td>
                    <td className="py-2 px-3 text-center">
                      <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold ${rc.bg}`}>
                        {t.result}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
