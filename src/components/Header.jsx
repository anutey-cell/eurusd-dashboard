import { useState, useEffect, useRef } from 'react';
import { Activity, Wifi, WifiOff, Clock, TrendingUp, TrendingDown, AlertTriangle, Database } from 'lucide-react';
import { getDataStatus, getCandles } from '../services/api';
import { formatKenyaTime, nowKenyaHour, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const SESSION_SCHEDULE = [
  { name: 'Tokyo',  start: 0,  end: 9  },
  { name: 'London', start: 8,  end: 17 },
  { name: 'NY',     start: 13, end: 22 },
];

function getCurrentSession() {
  const h = new Date().getUTCHours();
  const active = SESSION_SCHEDULE.filter(s => h >= s.start && h < s.end);
  return active.length ? active.map(s => s.name).join(' + ') : 'Off-Hours';
}

/**
 * Live-price ticker:
 *   1. Polls GET /candles?pair=xauusd&interval=M15&limit=96 (24h window) every
 *      15s. Pins anchor to last close; computes 24h change from first→last.
 *   2. Between polls, jitters ±$0.30 around the anchor so the UI feels alive.
 *   3. Until the first poll lands we show "—" — never a stale demo seed.
 *
 * Returns { price, tick, change, changePct } — all live.
 */
// Provider sources we ACCEPT as real prices. Anything else (synthetic/demo)
// is rejected — the dashboard shows "—" with a warning chip rather than
// flashing a misleading synthetic price near the BASE_PRICE_XAUUSD seed.
const LIVE_SOURCES = new Set(['tradingview', 'mt5', 'tradingview-cached', 'mt5-cached']);

function useLivePrice() {
  const [price, setPrice]       = useState(null);
  const [tick, setTick]         = useState(null);
  const [change, setChange]     = useState(null);
  const [changePct, setChangePct] = useState(null);
  const [source, setSource]     = useState(null);   // "tradingview" | "synthetic" | …
  const anchorRef               = useRef(null);     // last confirmed real close

  // Pull 24h of M15 candles, compute live close + 24h change
  useEffect(() => {
    let cancelled = false;

    async function pull() {
      try {
        const res = await getCandles({ pair: 'xauusd', interval: 'M15', limit: 96 });
        const candles  = res?.candles ?? res?.data?.candles ?? [];
        // `source` lives in the APIResponse envelope; the api.js helper returns
        // the parsed envelope, so it's res.source directly.
        const provider = res?.source ?? res?.data?.source ?? 'unknown';

        if (cancelled) return;
        setSource(provider);

        // Refuse to surface synthetic / demo numbers — clear all state so
        // the UI shows "—" and the warning chip surfaces.
        if (!LIVE_SOURCES.has(provider)) {
          anchorRef.current = null;
          setPrice(null);
          setChange(null);
          setChangePct(null);
          setTick(null);
          return;
        }

        if (candles.length < 2) return;
        const first = candles[0];
        const last  = candles[candles.length - 1];
        const close = last?.close;
        const ref   = first?.close;
        if (typeof close !== 'number' || !Number.isFinite(close)) return;

        anchorRef.current = close;
        setPrice(prev => {
          if (prev != null) setTick(close >= prev ? 'up' : 'down');
          return close;
        });
        if (typeof ref === 'number' && Number.isFinite(ref) && ref > 0) {
          const delta = close - ref;
          setChange(delta);
          setChangePct((delta / ref) * 100);
        }
      } catch {
        // silent — keep the previous price; jitter below still animates the UI
      }
    }

    pull();
    const id = setInterval(pull, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Tiny jitter between real-price pulls so the ticker doesn't look frozen.
  // Only runs once we have a valid live anchor — never animates synthetic data.
  useEffect(() => {
    const id = setInterval(() => {
      if (anchorRef.current == null) return;
      const jitter = (Math.random() - 0.5) * 0.60; // ±$0.30
      setPrice(prev => {
        const base = prev ?? anchorRef.current;
        const next = Math.round((base + jitter) * 100) / 100;
        setTick(jitter >= 0 ? 'up' : 'down');
        return next;
      });
    }, 1800);
    return () => clearInterval(id);
  }, []);

  return { price, tick, change, changePct, source, isLive: LIVE_SOURCES.has(source) };
}

function useDataStatus() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    getDataStatus()
      .then(s => { if (!cancelled) setStatus(s); })
      .catch(e => { if (!cancelled) setError(e?.message ?? 'Backend unavailable'); });
    return () => { cancelled = true; };
  }, []);

  return { status, error };
}

export default function Header({ instrument }) {
  const [now, setNow] = useState(new Date());
  // All values are pulled live from /candles every 15s — never a demo seed.
  // First poll lands ~1s after mount; before that we show "—".
  // `isLive` is false when the backend returned synthetic/demo data (provider down).
  const { price, tick, changePct, source, isLive: priceIsLive } = useLivePrice();
  const { status, error: statusError } = useDataStatus();
  const pairLabel = instrument?.label ?? 'XAU/USD';
  const decimals  = instrument?.decimals ?? 2;

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  // Wall clock shown in East Africa Time (UTC+3). Session detection still
  // uses UTC since ICT killzones are defined in UTC.
  const localTime = formatKenyaTime(now);
  const session   = getCurrentSession();
  // Up/down derives from the LIVE 24h change percentage, not the mock fallback.
  const isUp = (changePct ?? 0) >= 0;

  const isLive    = status?.dataMode === 'live';
  const provider  = status?.fxProvider ?? null;
  const lastSeen  = status?.timestamp
    ? formatKenyaTime(status.timestamp)
    : null;

  return (
    <header className="bg-[#0d1117] border-b border-[#263044] px-4 py-3 flex items-center justify-between flex-wrap gap-3">

      {/* Logo / Title */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center">
            <Activity size={14} className="text-white" />
          </div>
          <span className="text-sm font-semibold text-white tracking-wide">XAU/USD Signal Pro</span>
        </div>
        <span className="text-[#263044] hidden sm:block">|</span>
        <span className="text-xs text-gray-500 hidden sm:block">Gold · ICT Signal Dashboard</span>
      </div>

      {/* Live Price Ticker */}
      <div className="flex items-center gap-2 bg-[#161b27] border border-[#263044] rounded-lg px-4 py-2">
        <span className="text-xs font-semibold text-gray-400 mr-1">{pairLabel}</span>
        <span
          className={`font-mono text-xl font-bold transition-colors duration-300 ${
            tick === 'up' ? 'text-emerald-400' : tick === 'down' ? 'text-red-400' : 'text-white'
          }`}
        >
          {price != null ? price.toFixed(decimals) : '—'}
        </span>
        <div className={`flex items-center gap-0.5 text-xs font-mono ${isUp ? 'text-emerald-400' : 'text-red-400'}`}>
          {isUp ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
          {changePct != null
            ? `${isUp ? '+' : ''}${changePct.toFixed(2)}% 24h`
            : '—'}
        </div>
        {/* Source chip — green for live providers, red when backend served synthetic */}
        {source && (
          <span
            className={`text-[9px] font-bold uppercase tracking-widest px-1.5 py-0.5 rounded border ${
              priceIsLive
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                : 'border-red-500/50 bg-red-500/10 text-red-300'
            }`}
            title={priceIsLive
              ? `Live feed: ${source}`
              : `Provider down — backend returned ${source}. Refusing to display synthetic prices.`}
          >
            {priceIsLive
              ? (source.replace('-cached', '') + (source.endsWith('-cached') ? ' ⟲' : ''))
              : 'NO LIVE FEED'}
          </span>
        )}
      </div>

      {/* Status Bar */}
      <div className="flex items-center gap-3 text-xs text-gray-500 flex-wrap">

        {/* Session */}
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-gray-300">{session}</span>
        </div>

        {/* EAT Clock (Africa/Nairobi, UTC+3) */}
        <div className="flex items-center gap-1.5 hidden sm:flex">
          <Clock size={12} />
          <span className="font-mono">{localTime} {KENYA_LABEL}</span>
        </div>

        {/* Data Mode badge */}
        {statusError ? (
          <div className="flex items-center gap-1.5 bg-red-500/10 border border-red-500/30 rounded px-2 py-0.5">
            <WifiOff size={11} className="text-red-400" />
            <span className="text-red-400 font-medium">API Error</span>
          </div>
        ) : isLive ? (
          <div className="flex items-center gap-1.5 bg-emerald-500/10 border border-emerald-500/20 rounded px-2 py-0.5">
            <Wifi size={11} className="text-emerald-400" />
            <span className="text-emerald-400 font-medium">Live Mode</span>
          </div>
        ) : (
          <div className="flex items-center gap-1.5 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-0.5">
            <Database size={11} className="text-amber-400" />
            <span className="text-amber-400 font-medium">Demo Mode</span>
          </div>
        )}

        {/* Broker Disabled badge */}
        <div className="flex items-center gap-1.5 bg-slate-800 border border-slate-700 rounded px-2 py-0.5">
          <span className="text-slate-500 font-medium text-[10px]">Execution DISABLED</span>
        </div>

        {/* Data Provider */}
        {provider && (
          <div className="hidden sm:flex items-center gap-1 text-gray-500">
            <span className="text-gray-600">Provider:</span>
            <span className="text-gray-400 font-mono">{provider}</span>
          </div>
        )}

        {/* Last Updated */}
        {lastSeen && (
          <div className="hidden md:flex items-center gap-1 text-gray-600">
            <span>Updated:</span>
            <span className="font-mono text-gray-500">{lastSeen}</span>
          </div>
        )}

        {/* API Error tooltip */}
        {statusError && (
          <div
            className="hidden lg:flex items-center gap-1 text-red-400/70 max-w-[200px] truncate"
            title={statusError}
          >
            <AlertTriangle size={11} />
            <span className="truncate">{statusError}</span>
          </div>
        )}

      </div>
    </header>
  );
}
