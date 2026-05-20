/**
 * IntermarketCorrelationPanel
 *
 * Live correlations of XAU/USD vs DXY, US10Y, WTI, VIX, Silver, S&P 500.
 * Detects when historical relationships break down — the strategist treats
 * this as a regime-shift signal (e.g., gold + DXY both rising = fear bid).
 */
import { useState, useCallback } from 'react';
import {
  Compass, RefreshCw, AlertTriangle, CheckCircle, XCircle, TrendingUp,
} from 'lucide-react';
import { getIntermarketCorrelations } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 5 * 60_000;

const STATUS_CFG = {
  aligned:      { cls: 'text-emerald-400', label: 'aligned',     Icon: CheckCircle },
  weakened:     { cls: 'text-amber-400',   label: 'weakened',    Icon: TrendingUp },
  regime_shift: { cls: 'text-red-400',     label: 'regime shift',Icon: XCircle },
  no_data:      { cls: 'text-gray-500',    label: 'no data',     Icon: XCircle },
  insufficient_overlap: { cls: 'text-gray-500', label: 'low overlap', Icon: XCircle },
};

function corrCell(v) {
  if (v == null) return <span className="text-gray-600">—</span>;
  const cls = v > 0.4 ? 'text-emerald-400'
            : v > 0   ? 'text-emerald-300'
            : v > -0.4 ? 'text-red-300'
            :           'text-red-400';
  return <span className={`font-mono ${cls}`}>{v > 0 ? '+' : ''}{v.toFixed(2)}</span>;
}

export default function IntermarketCorrelationPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await getIntermarketCorrelations({ timeframe: 'H1', nBars: 200 });
      setData(d);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Correlations unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(load, POLL_MS);

  return (
    <div className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Compass size={16} className="text-cyan-400" />
          <h2 className="text-sm font-semibold text-white tracking-wide">
            Intermarket Correlations — XAU/USD
          </h2>
          <span className="text-[10px] text-gray-500 hidden sm:inline">
            DXY · US10Y · Oil · VIX · Silver · S&P  — regime detector
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
          <AlertTriangle size={11} /> {error}
        </div>
      )}

      {data && data.pairs && (
        <>
          {/* Alignment score banner */}
          <div className={`rounded border-2 p-3 ${
            data.intermarket_alignment_score >= 80
              ? 'border-emerald-500/50 bg-emerald-500/10'
              : data.intermarket_alignment_score >= 60
                ? 'border-amber-500/40 bg-amber-500/10'
                : 'border-red-500/50 bg-red-500/10'
          }`}>
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div>
                <div className="text-[10px] uppercase tracking-widest opacity-80">
                  Intermarket Alignment
                </div>
                <div className="text-2xl font-mono font-bold">
                  {data.intermarket_alignment_score}%
                </div>
              </div>
              <div className="text-[11px] max-w-md text-right opacity-90">
                {data.interpretation}
              </div>
            </div>
          </div>

          {/* Per-pair table */}
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[10px] uppercase tracking-widest text-gray-500 border-b border-[#263044]">
                  <th className="text-left py-2">Pair</th>
                  <th className="text-right">Expected</th>
                  <th className="text-right">20-bar</th>
                  <th className="text-right">60-bar</th>
                  <th className="text-right">100-bar</th>
                  <th className="text-center">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.pairs.map(p => {
                  const sc = STATUS_CFG[p.status] ?? STATUS_CFG.no_data;
                  return (
                    <tr key={p.code} className="border-b border-[#1c2333] hover:bg-[#1c2333]/40">
                      <td className="py-2 text-gray-200">{p.label}</td>
                      <td className="text-right font-mono text-gray-500">
                        {p.expected != null ? (p.expected > 0 ? '+' : '') + p.expected.toFixed(2) : '—'}
                      </td>
                      <td className="text-right">{corrCell(p.corr_20)}</td>
                      <td className="text-right">{corrCell(p.corr_60)}</td>
                      <td className="text-right">{corrCell(p.corr_100)}</td>
                      <td className={`text-center ${sc.cls}`}>
                        <div className="inline-flex items-center gap-1">
                          <sc.Icon size={10} />
                          <span className="text-[9px] uppercase tracking-widest font-bold">{sc.label}</span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="text-[10px] text-gray-600 text-right">
            {data.timeframe} · {data.xauusd_bars} XAUUSD bars · Updated {formatKenyaTime(data.generated_at)} {KENYA_LABEL}
          </div>
        </>
      )}
    </div>
  );
}
