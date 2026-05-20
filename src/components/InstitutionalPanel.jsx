/**
 * InstitutionalPanel — LIVE flow for XAU/USD.
 *
 * Source: GET /api/v1/institutional (replaces the old mockData.institutionalData
 * which had stale EUR/USD-era prices like 1.09000, 1.08780).
 *
 * Shows:
 *   - CFTC COT positioning (commercials vs large specs, weekly change, bias)
 *   - Live key price levels (swing highs/lows, recent FVG, daily range)
 *   - MyFxBook retail sentiment when configured
 *
 * Anything without a real data source is OMITTED, not faked.
 */
import { useState, useEffect, useCallback } from 'react';
import {
  Building2, TrendingUp, TrendingDown, RefreshCw, AlertTriangle,
  Layers, BarChart, Target, Calendar, Eye,
} from 'lucide-react';
import { getInstitutional } from '../services/api';
import { formatKenyaDateTime, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 5 * 60_000;   // 5 min — COT only updates weekly anyway

// ── Sub: COT card ──────────────────────────────────────────────────────────
function CotCard({ cot }) {
  if (!cot || cot.source === 'unavailable' || cot.source === 'demo_fallback') {
    return (
      <Block title="CFTC COT (weekly)" icon={Calendar}>
        <Unavailable
          msg={cot?.error || 'CFTC fetch failed — provider returned no data this week'}
        />
      </Block>
    );
  }
  const bias = cot.bias || 'Neutral';
  const biasCls = bias === 'Bullish' ? 'text-emerald-400'
                : bias === 'Bearish' ? 'text-red-400'
                : 'text-gray-400';
  const change = cot.change ?? 0;
  return (
    <Block title="CFTC COT (weekly)" icon={Calendar} subtitle={`Source: ${cot.source}`}>
      <Row label="As of"            value={cot.asOf || '—'} />
      <Row label="Net position"     value={cot.netPosition?.toLocaleString() ?? '—'} valueCls={cot.netPosition >= 0 ? 'text-emerald-400' : 'text-red-400'} />
      <Row label="Long contracts"   value={cot.longContracts?.toLocaleString() ?? '—'} valueCls="text-emerald-300" />
      <Row label="Short contracts"  value={cot.shortContracts?.toLocaleString() ?? '—'} valueCls="text-red-300" />
      <Row
        label="Weekly change"
        value={`${change >= 0 ? '+' : ''}${change.toLocaleString()}`}
        valueCls={change >= 0 ? 'text-emerald-400' : 'text-red-400'}
      />
      <Row label="Bias" value={bias} valueCls={biasCls + ' font-bold'} />
    </Block>
  );
}

// ── Sub: Key price levels card ─────────────────────────────────────────────
function LevelsCard({ levels }) {
  const liveSources = new Set(['tradingview', 'mt5', 'tradingview-cached', 'mt5-cached']);
  const src = levels?.data_source;
  if (!levels || !liveSources.has(src)) {
    return (
      <Block title="Institutional Price Levels" icon={Target}>
        <Unavailable
          msg={levels?.error || `Refusing to display levels from ${src ?? 'unknown'} source — live feed required.`}
        />
      </Block>
    );
  }
  const cur = levels.current_price;
  return (
    <Block title="Institutional Price Levels" icon={Target} subtitle={`Computed from ${src} H4 candles`}>
      {cur != null && <Row label="Current price"     value={`$${cur.toFixed(2)}`} valueCls="text-white font-bold" />}
      {levels.daily_open      != null && <Row label="Daily open"        value={`$${levels.daily_open.toFixed(2)}`} />}
      {levels.daily_high      != null && <Row label="Today's high"      value={`$${levels.daily_high.toFixed(2)}`} valueCls="text-emerald-300" />}
      {levels.daily_low       != null && <Row label="Today's low"       value={`$${levels.daily_low.toFixed(2)}`} valueCls="text-red-300" />}
      {levels.prev_daily_high != null && <Row label="Prev daily high"   value={`$${levels.prev_daily_high.toFixed(2)} (liquidity above)`} valueCls="text-emerald-300" />}
      {levels.prev_daily_low  != null && <Row label="Prev daily low"    value={`$${levels.prev_daily_low.toFixed(2)} (liquidity below)`} valueCls="text-red-300" />}
    </Block>
  );
}

// ── Sub: FVG card ──────────────────────────────────────────────────────────
function FvgCard({ levels }) {
  if (!levels?.last_fvg_bull && !levels?.last_fvg_bear) return null;
  return (
    <Block title="Recent Fair Value Gaps" icon={Layers} subtitle="Imbalances institutions may rebalance">
      {levels.last_fvg_bull && (
        <div className="rounded border border-emerald-500/30 bg-emerald-500/5 p-2 mb-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] uppercase tracking-widest text-emerald-300 font-bold">Bullish FVG</span>
            <span className="text-[10px] text-gray-500">
              {levels.last_fvg_bull.filled ? 'FILLED' : 'unfilled'}
            </span>
          </div>
          <Row label="Zone"    value={`$${levels.last_fvg_bull.lower.toFixed(2)} - $${levels.last_fvg_bull.upper.toFixed(2)}`} />
          <Row label="Mid"     value={`$${levels.last_fvg_bull.mid.toFixed(2)}`} />
        </div>
      )}
      {levels.last_fvg_bear && (
        <div className="rounded border border-red-500/30 bg-red-500/5 p-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] uppercase tracking-widest text-red-300 font-bold">Bearish FVG</span>
            <span className="text-[10px] text-gray-500">
              {levels.last_fvg_bear.filled ? 'FILLED' : 'unfilled'}
            </span>
          </div>
          <Row label="Zone" value={`$${levels.last_fvg_bear.lower.toFixed(2)} - $${levels.last_fvg_bear.upper.toFixed(2)}`} />
          <Row label="Mid"  value={`$${levels.last_fvg_bear.mid.toFixed(2)}`} />
        </div>
      )}
    </Block>
  );
}

// ── Sub: Swing pivots card (institutional liquidity pools) ─────────────────
function SwingsCard({ levels }) {
  const highs = levels?.swing_highs || [];
  const lows  = levels?.swing_lows  || [];
  if (!highs.length && !lows.length) return null;
  return (
    <Block title="Recent Swing Pivots" icon={BarChart} subtitle="Liquidity pools — stops parked here">
      {highs.length > 0 && (
        <div className="mb-2">
          <div className="text-[10px] uppercase tracking-widest text-emerald-300 mb-1">Swing highs</div>
          {highs.map((h, i) => (
            <Row key={`h${i}`} label={new Date(h.time).toLocaleDateString()} value={`$${h.price.toFixed(2)}`} valueCls="text-emerald-300" />
          ))}
        </div>
      )}
      {lows.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-widest text-red-300 mb-1">Swing lows</div>
          {lows.map((l, i) => (
            <Row key={`l${i}`} label={new Date(l.time).toLocaleDateString()} value={`$${l.price.toFixed(2)}`} valueCls="text-red-300" />
          ))}
        </div>
      )}
    </Block>
  );
}

// ── Sub: Sentiment card (MyFxBook) ─────────────────────────────────────────
function SentimentCard({ sentiment }) {
  if (!sentiment) return null;
  const ratio = sentiment.longPct ?? sentiment.long_percent ?? sentiment.longPercent;
  if (ratio == null) return null;
  const sellRatio = 100 - ratio;
  return (
    <Block title="MyFxBook Retail Sentiment" icon={Eye}>
      <div className="flex justify-between text-[10px] mb-1">
        <span className="text-emerald-400 font-medium">Long {ratio}%</span>
        <span className="text-red-400 font-medium">Short {sellRatio}%</span>
      </div>
      <div className="flex h-2 rounded-full overflow-hidden">
        <div className="bg-emerald-500" style={{ width: `${ratio}%` }} />
        <div className="bg-red-500 flex-1" />
      </div>
      <div className="text-[9px] text-gray-500 mt-1.5">
        Retail crowd. Engine treats extremes (&gt;70/&lt;30) as contrarian.
      </div>
    </Block>
  );
}

// ── Layout helpers ─────────────────────────────────────────────────────────
function Block({ title, icon: Icon, subtitle, children }) {
  return (
    <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3 flex-1 min-w-[240px]">
      <div className="flex items-center gap-1.5 mb-2">
        {Icon && <Icon size={11} className="text-purple-400" />}
        <span className="text-[10px] uppercase tracking-widest text-gray-300 font-bold">{title}</span>
      </div>
      {subtitle && <div className="text-[9px] text-gray-600 mb-2">{subtitle}</div>}
      <div className="space-y-0">{children}</div>
    </div>
  );
}

function Row({ label, value, valueCls = 'text-white' }) {
  return (
    <div className="flex justify-between items-center py-1 border-b border-[#1e2535] last:border-0">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-xs font-mono font-medium ${valueCls}`}>{value}</span>
    </div>
  );
}

function Unavailable({ msg }) {
  return (
    <div className="flex items-start gap-2 text-[10px] text-amber-300 leading-tight py-2">
      <AlertTriangle size={11} className="shrink-0 mt-0.5" />
      <span>{msg}</span>
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────
export default function InstitutionalPanel() {
  const [data, setData]       = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await getInstitutional();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Institutional fetch failed');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(load, POLL_MS);

  return (
    <div className="card flex flex-col gap-0">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <Building2 size={13} className="text-purple-400" />
          <span className="card-title">Institutional Flow · XAU/USD</span>
          {data?.providers && (
            <span className="text-[9px] text-gray-600">
              COT={data.providers.cot} · levels={data.providers.levels}
              {data.providers.sentiment !== 'unavailable' && ` · sent=${data.providers.sentiment}`}
            </span>
          )}
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-300"
        >
          <RefreshCw size={10} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      <div className="card-body">
        {error && (
          <div className="bg-red-500/10 border border-red-500/30 rounded p-2 text-[10px] text-red-300 flex items-center gap-1 mb-3">
            <AlertTriangle size={10} />
            {error}
          </div>
        )}

        {data && (
          <div className="flex flex-wrap gap-3">
            <CotCard      cot={data.cot} />
            <LevelsCard   levels={data.levels} />
            <FvgCard      levels={data.levels} />
            <SwingsCard   levels={data.levels} />
            <SentimentCard sentiment={data.sentiment} />
          </div>
        )}

        {data?.generated_at && (
          <div className="text-[10px] text-gray-600 text-right mt-2">
            Updated {formatKenyaDateTime(data.generated_at)} {KENYA_LABEL}
          </div>
        )}
      </div>
    </div>
  );
}
