/**
 * InstitutionalScanPanel
 *
 * Automatic XAU/USD opportunity scanner with full institutional market view.
 *
 * Features:
 *   - "Scan Institutional Setup" button  → POST /scan/xauusd/force
 *   - Auto-refresh every VITE_SCAN_INTERVAL_SECONDS (default 60 s)
 *   - Market state badge, institutional bias, confidence score
 *   - Written institutional analyst narrative
 *   - Multi-timeframe model (D1 / H4 / H1 / M15 / M5)
 *   - Opportunity status, blockers, key drivers
 *   - Liquidity view, FVG view, news & risk panel
 *
 * Safety:
 *   - Does NOT place trades, confirm signals, or send MT5 orders
 *   - Signal engine remains the final BUY/SELL gate
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import {
  ScanLine, RefreshCw, AlertTriangle, CheckCircle, XCircle,
  Clock, TrendingUp, TrendingDown, Minus, Eye, Zap,
  Shield, Activity, Target, BarChart2, Layers,
  ChevronDown, ChevronUp, BookOpen,
} from 'lucide-react';
import { getScan, forceScan } from '../services/api';

// ── Constants ─────────────────────────────────────────────────────────────────

const SCAN_INTERVAL_MS = parseInt(
  import.meta.env.VITE_SCAN_INTERVAL_SECONDS ?? '60', 10
) * 1000;

// ── Colour helpers ────────────────────────────────────────────────────────────

const STATE_CONFIG = {
  SIGNAL_READY:        { cls: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40', label: 'Signal Ready',         dot: 'bg-emerald-400' },
  SETUP_FORMING:       { cls: 'bg-blue-500/20   text-blue-400   border-blue-500/40',     label: 'Setup Forming',        dot: 'bg-blue-400'    },
  WATCHLIST:           { cls: 'bg-amber-500/20  text-amber-400  border-amber-500/40',    label: 'Watchlist',            dot: 'bg-amber-400'   },
  NO_TRADE:            { cls: 'bg-gray-500/20   text-gray-400   border-gray-500/40',     label: 'No Trade',             dot: 'bg-gray-600'    },
  NEWS_BLOCKED:        { cls: 'bg-red-500/20    text-red-400    border-red-500/40',      label: 'News Blocked',         dot: 'bg-red-400'     },
  DATA_STALE:          { cls: 'bg-orange-500/20 text-orange-400 border-orange-500/40',   label: 'Data Stale',           dot: 'bg-orange-400'  },
  SPREAD_TOO_WIDE:     { cls: 'bg-red-500/20    text-red-400    border-red-500/40',      label: 'Spread Too Wide',      dot: 'bg-red-400'     },
  VOLATILITY_UNSTABLE: { cls: 'bg-purple-500/20 text-purple-400 border-purple-500/40',   label: 'Volatility Unstable',  dot: 'bg-purple-400'  },
};

const BIAS_COLOR = {
  Bullish:           'text-emerald-400',
  Bearish:           'text-red-400',
  Neutral:           'text-gray-400',
  'Range-bound':     'text-amber-400',
  Compression:       'text-amber-400',
  Expansion:         'text-blue-400',
  'Reversal forming':'text-purple-400',
};

const SIGNAL_CONFIG = {
  BUY:  { Icon: TrendingUp,   cls: 'text-emerald-400' },
  SELL: { Icon: TrendingDown, cls: 'text-red-400'     },
  WAIT: { Icon: Minus,        cls: 'text-amber-400'   },
};

// ── Small reusable sub-components ─────────────────────────────────────────────

function StateBadge({ state }) {
  const cfg = STATE_CONFIG[state] ?? STATE_CONFIG.NO_TRADE;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-semibold ${cfg.cls}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
      {cfg.label}
    </span>
  );
}

function BiasChip({ bias }) {
  const col = BIAS_COLOR[bias] ?? 'text-gray-400';
  return <span className={`font-semibold text-sm ${col}`}>{bias}</span>;
}

function ConfidenceBar({ value }) {
  const pct  = Math.max(0, Math.min(100, value ?? 0));
  const col  = pct >= 80 ? 'bg-emerald-500' : pct >= 60 ? 'bg-amber-500' : pct >= 40 ? 'bg-blue-500' : 'bg-gray-600';
  return (
    <div>
      <div className="flex justify-between text-[10px] text-gray-500 mb-1">
        <span>Confidence</span>
        <span className="font-mono text-gray-300">{pct}%</span>
      </div>
      <div className="h-1.5 bg-[#263044] rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all duration-500 ${col}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function SectionHeader({ Icon, label }) {
  return (
    <div className="flex items-center gap-2 mb-2">
      <Icon size={12} className="text-blue-400 flex-shrink-0" />
      <span className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">{label}</span>
    </div>
  );
}

function InfoRow({ label, value, valueClass = 'text-gray-300' }) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-[#1e2535] last:border-0">
      <span className="text-[10px] text-gray-500">{label}</span>
      <span className={`text-xs font-medium ${valueClass}`}>{value ?? '—'}</span>
    </div>
  );
}

function BlockerRow({ code, message }) {
  return (
    <div className="flex items-start gap-2 py-1">
      <XCircle size={11} className="text-red-400 flex-shrink-0 mt-0.5" />
      <div>
        <span className="text-[9px] text-red-400/70 font-mono">{code}</span>
        <p className="text-[10px] text-gray-400 leading-tight">{message}</p>
      </div>
    </div>
  );
}

function DriverRow({ text }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      <CheckCircle size={10} className="text-blue-400 flex-shrink-0 mt-0.5" />
      <span className="text-[10px] text-gray-400 leading-tight">{text}</span>
    </div>
  );
}

function CollapsibleSection({ title, Icon, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border border-[#1e2535] rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-3 py-2 bg-[#131c27] hover:bg-[#1a2436] transition-colors"
      >
        <div className="flex items-center gap-2">
          <Icon size={11} className="text-blue-400" />
          <span className="text-[10px] text-slate-400 uppercase tracking-widest font-semibold">{title}</span>
        </div>
        {open ? <ChevronUp size={11} className="text-gray-600" /> : <ChevronDown size={11} className="text-gray-600" />}
      </button>
      {open && <div className="px-3 py-2.5 bg-[#0d1117]">{children}</div>}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function InstitutionalScanPanel({ onScanComplete }) {
  const [scan,       setScan]       = useState(null);
  const [scanning,   setScanning]   = useState(false);
  const [error,      setError]      = useState(null);
  const [lastAt,     setLastAt]     = useState(null);
  const [countdown,  setCountdown]  = useState(SCAN_INTERVAL_MS / 1000);
  const timerRef = useRef(null);

  // ── Auto-fetch (silent, no spinner) ───────────────────────────────────────
  const fetchScan = useCallback(async (force = false, showLoading = true) => {
    if (force) {
      setScanning(true);
      setError(null);
    }
    try {
      const data = force ? await forceScan() : await getScan();
      setScan(data);
      setLastAt(new Date());
      setCountdown(SCAN_INTERVAL_MS / 1000);
      if (force) onScanComplete?.(data);
    } catch (e) {
      setError(e.message ?? 'Scan failed');
    } finally {
      if (force) setScanning(false);
    }
  }, [onScanComplete]);

  // Initial load
  useEffect(() => { fetchScan(false, false); }, [fetchScan]);

  // Auto-refresh interval
  useEffect(() => {
    const id = setInterval(() => fetchScan(false, false), SCAN_INTERVAL_MS);
    return () => clearInterval(id);
  }, [fetchScan]);

  // Countdown ticker
  useEffect(() => {
    const id = setInterval(() => setCountdown(c => c <= 1 ? SCAN_INTERVAL_MS / 1000 : c - 1), 1000);
    return () => clearInterval(id);
  }, [lastAt]);

  // ── Derived values ────────────────────────────────────────────────────────
  const state  = scan?.marketState    ?? 'NO_TRADE';
  const signal = scan?.signal         ?? 'WAIT';
  const bias   = scan?.institutionalBias ?? 'Neutral';
  const conf   = scan?.confidence     ?? 0;
  const summ   = scan?.summary        ?? '';
  const opp    = scan?.opportunity    ?? {};
  const model  = scan?.model          ?? {};
  const liq    = scan?.liquidity      ?? {};
  const fvg    = scan?.fvg            ?? {};
  const news   = scan?.news           ?? {};
  const risk   = scan?.risk           ?? {};
  const blockers    = scan?.blockers     ?? [];
  const blockerCodes = scan?.blockerCodes ?? [];
  const keyDrivers  = scan?.keyDrivers   ?? [];

  const SigCfg = SIGNAL_CONFIG[signal] ?? SIGNAL_CONFIG.WAIT;
  const isReady = state === 'SIGNAL_READY';

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl overflow-hidden">

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#263044] bg-[#131c27]">
        <div className="flex items-center gap-2.5">
          <ScanLine size={15} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
            Institutional Scanner — XAU/USD
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {lastAt && (
            <span className="text-[10px] text-gray-600 font-mono hidden sm:block">
              Updated {lastAt.toLocaleTimeString()} · next in {countdown}s
            </span>
          )}
          <span className="text-[9px] text-slate-600 bg-slate-800/60 px-2 py-0.5 rounded-full border border-slate-700">
            Analysis only · No execution
          </span>
        </div>
      </div>

      <div className="p-5 space-y-5">

        {/* ── Scan Button ───────────────────────────────────────────────── */}
        <button
          onClick={() => fetchScan(true)}
          disabled={scanning}
          className={`w-full py-3.5 rounded-xl font-bold text-sm flex items-center justify-center gap-2.5 transition-all
            ${scanning
              ? 'bg-blue-900/40 text-blue-300/60 cursor-not-allowed'
              : isReady
                ? 'bg-emerald-700 hover:bg-emerald-600 text-white shadow-lg shadow-emerald-900/20'
                : 'bg-blue-700 hover:bg-blue-600 text-white shadow-lg shadow-blue-900/20'
            }`}
        >
          {scanning
            ? <><RefreshCw size={15} className="animate-spin" /> Scanning institutional setup…</>
            : <><ScanLine size={15} /> Scan Institutional Setup</>
          }
        </button>

        {error && (
          <div className="flex items-center gap-2 text-xs text-red-400 bg-red-900/20 border border-red-800/40 rounded-lg px-3 py-2">
            <AlertTriangle size={13} />
            {error}
          </div>
        )}

        {/* ── Market State + Bias + Confidence ──────────────────────────── */}
        <div className="bg-[#131c27] rounded-xl p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="space-y-1">
              <StateBadge state={state} />
              <div className="flex items-center gap-2 mt-1.5">
                <SigCfg.Icon size={14} className={SigCfg.cls} />
                <span className={`font-black text-lg tracking-widest ${SigCfg.cls}`}>{signal}</span>
                <span className="text-gray-600">·</span>
                <BiasChip bias={bias} />
              </div>
            </div>
            <div className="text-right">
              <div className={`text-3xl font-black font-mono ${conf >= 80 ? 'text-emerald-400' : conf >= 60 ? 'text-amber-400' : conf >= 40 ? 'text-blue-400' : 'text-gray-500'}`}>
                {conf}
              </div>
              <div className="text-[9px] text-gray-500 uppercase tracking-wider">/100 · confidence</div>
            </div>
          </div>
          <ConfidenceBar value={conf} />
          {scan?.readiness && (
            <div className="text-[10px] text-gray-500 uppercase tracking-widest">
              Readiness: <span className="text-gray-300 normal-case font-medium">{scan.readiness}</span>
            </div>
          )}
        </div>

        {/* ── Market Narrative ──────────────────────────────────────────── */}
        {summ && (
          <CollapsibleSection title="Institutional Market View" Icon={BookOpen} defaultOpen={true}>
            <p className="text-xs text-gray-300 leading-relaxed">{summ}</p>
            {scan?.action && (
              <div className="mt-2 flex items-center gap-1.5 text-[10px] text-blue-300/70 font-mono bg-blue-900/10 rounded px-2 py-1">
                <Target size={10} />
                Action: {scan.action.replace(/_/g, ' ')}
              </div>
            )}
          </CollapsibleSection>
        )}

        {/* ── Opportunity Status ────────────────────────────────────────── */}
        <CollapsibleSection title="Opportunity Status" Icon={Target} defaultOpen={true}>
          <div className="space-y-1">
            <InfoRow label="Status"     value={opp.status}       valueClass={opp.status === 'SIGNAL_READY' ? 'text-emerald-400 font-bold' : opp.status === 'SETUP_FORMING' ? 'text-blue-400' : opp.status === 'WATCHLIST' ? 'text-amber-400' : 'text-gray-400'} />
            <InfoRow label="Side"       value={opp.side}         valueClass={opp.side === 'BUY' ? 'text-emerald-400 font-bold' : opp.side === 'SELL' ? 'text-red-400 font-bold' : 'text-gray-500'} />
            <InfoRow label="Score"      value={opp.qualityScore !== undefined ? `${opp.qualityScore}/100` : '—'} />
            <InfoRow label="Entry zone" value={opp.entryZone} />
            <InfoRow label="Target"     value={opp.target} />
            <InfoRow label="R:R"        value={opp.rrStatus} />
            <InfoRow label="Invalidation" value={opp.invalidation} valueClass="text-red-400/70 text-[10px]" />
          </div>
        </CollapsibleSection>

        {/* ── Multi-TF Model ────────────────────────────────────────────── */}
        <CollapsibleSection title="Institutional Model" Icon={Layers} defaultOpen={true}>
          <div className="space-y-1">
            <InfoRow label="D1 Bias"       value={model.d1Bias}       valueClass={_biasClass(model.d1Bias)} />
            <InfoRow label="H4 Bias"       value={model.h4Bias}       valueClass={_biasClass(model.h4Bias)} />
            <InfoRow label="H1 Bias"       value={model.h1Bias}       valueClass={_biasClass(model.h1Bias)} />
            <InfoRow label="M15 Structure" value={model.m15Structure} />
            <InfoRow label="M5 Execution"  value={model.m5Execution}  valueClass={isReady ? 'text-emerald-400' : 'text-amber-400'} />
          </div>
        </CollapsibleSection>

        {/* ── Key Drivers + Blockers ────────────────────────────────────── */}
        {(keyDrivers.length > 0 || blockers.length > 0) && (
          <CollapsibleSection title="Drivers & Blockers" Icon={Activity} defaultOpen={false}>
            {keyDrivers.length > 0 && (
              <div className="mb-3">
                <p className="text-[9px] text-gray-600 uppercase tracking-wider mb-1.5">Key Drivers</p>
                {keyDrivers.map((d, i) => <DriverRow key={i} text={d} />)}
              </div>
            )}
            {blockers.length > 0 && (
              <div>
                <p className="text-[9px] text-gray-600 uppercase tracking-wider mb-1.5">Blockers</p>
                {blockers.map((msg, i) => (
                  <BlockerRow key={i} code={blockerCodes[i] ?? '—'} message={msg} />
                ))}
              </div>
            )}
          </CollapsibleSection>
        )}

        {/* ── Liquidity View ────────────────────────────────────────────── */}
        <CollapsibleSection title="Liquidity View" Icon={BarChart2} defaultOpen={false}>
          <div className="space-y-1">
            <InfoRow label="Status"          value={liq.status} />
            <InfoRow label="Buy-side pool"   value={liq.buySide} />
            <InfoRow label="Sell-side pool"  value={liq.sellSide} />
            <InfoRow label="Swept"           value={liq.swept ? 'Yes' : 'No'} valueClass={liq.swept ? 'text-emerald-400' : 'text-gray-500'} />
            <InfoRow label="Sweep side"      value={liq.sweepSide} />
            <InfoRow label="Next target"     value={liq.nearestLiquidityTarget} />
          </div>
        </CollapsibleSection>

        {/* ── FVG View ──────────────────────────────────────────────────── */}
        <CollapsibleSection title="Fair Value Gap" Icon={Zap} defaultOpen={false}>
          <div className="space-y-1">
            <InfoRow label="Status"    value={fvg.status} />
            <InfoRow label="Direction" value={fvg.direction}
              valueClass={fvg.direction === 'Bullish' ? 'text-emerald-400' : fvg.direction === 'Bearish' ? 'text-red-400' : 'text-gray-400'} />
            <InfoRow label="Freshness" value={fvg.freshness} />
            <InfoRow label="Mitigated" value={fvg.mitigated ? 'Yes' : 'No'} valueClass={fvg.mitigated ? 'text-amber-400' : 'text-gray-400'} />
            {fvg.zone && (
              <>
                <InfoRow label="Zone high" value={fvg.zone.high?.toFixed(2)} valueClass="font-mono text-blue-300" />
                <InfoRow label="Zone low"  value={fvg.zone.low?.toFixed(2)}  valueClass="font-mono text-blue-300" />
              </>
            )}
          </div>
        </CollapsibleSection>

        {/* ── News & Risk ───────────────────────────────────────────────── */}
        <CollapsibleSection title="News & Risk" Icon={Shield} defaultOpen={false}>
          <div className="space-y-1">
            <InfoRow label="News status"    value={news.status}
              valueClass={news.status === 'BLOCKED' ? 'text-red-400 font-bold' : 'text-emerald-400'} />
            {news.blockingEvent && (
              <InfoRow label="Blocking event" value={news.blockingEvent} valueClass="text-red-400/80" />
            )}
            {news.nextEvent && (
              <>
                <InfoRow label="Next event"     value={news.nextEvent} />
                <InfoRow label="Time to event"  value={news.minutesToEvent != null ? `${news.minutesToEvent} min` : '—'} />
              </>
            )}
            <div className="border-t border-[#263044] mt-1.5 pt-1.5 space-y-1">
              <InfoRow label="Spread"     value={risk.spreadStatus}
                valueClass={risk.spreadStatus === 'OK' ? 'text-emerald-400' : 'text-red-400'} />
              <InfoRow label="Volatility" value={risk.volatilityStatus}
                valueClass={risk.volatilityStatus === 'OK' ? 'text-emerald-400' : risk.volatilityStatus === 'LOW' ? 'text-amber-400' : 'text-red-400'} />
              <InfoRow label="ATR (H4)"   value={risk.atr != null ? `${risk.atr} pts` : '—'} />
              <InfoRow label="Target realism" value={risk.targetRealism}
                valueClass={risk.targetRealism === 'REALISTIC' ? 'text-emerald-400' : risk.targetRealism === 'MARGINAL' ? 'text-amber-400' : 'text-red-400'} />
              <InfoRow label="Data status" value={risk.dataStatus}
                valueClass={risk.dataStatus === 'FRESH' ? 'text-emerald-400' : 'text-red-400'} />
              <InfoRow label="Session"    value={risk.session} />
            </div>
          </div>
        </CollapsibleSection>

        {/* ── Safety footer ─────────────────────────────────────────────── */}
        <div className="flex items-center gap-2 text-[9px] text-slate-600 bg-slate-900/40 rounded-lg px-3 py-2 border border-slate-800">
          <Shield size={9} className="flex-shrink-0" />
          Scanner is analysis-only. No trades are placed. Manual confirmation required.
        </div>

      </div>
    </section>
  );
}

// ── Utility ───────────────────────────────────────────────────────────────────

function _biasClass(text = '') {
  const t = text.toLowerCase();
  if (t.includes('bullish')) return 'text-emerald-400';
  if (t.includes('bearish')) return 'text-red-400';
  if (t.includes('neutral') || t.includes('mixed')) return 'text-gray-400';
  return 'text-gray-300';
}
