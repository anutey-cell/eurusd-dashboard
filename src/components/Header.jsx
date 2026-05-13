import { useState, useEffect } from 'react';
import { Activity, Wifi, WifiOff, Clock, TrendingUp, TrendingDown, AlertTriangle, Database } from 'lucide-react';
import { currentSignal } from '../data/mockData';
import { getDataStatus } from '../services/api';

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

function useSimulatedPrice(basePrice) {
  const [price, setPrice] = useState(basePrice);
  const [tick, setTick] = useState(null);
  useEffect(() => {
    const id = setInterval(() => {
      const delta = (Math.random() - 0.5) * 0.0004;
      setPrice(prev => {
        const next = Math.round((prev + delta) * 100000) / 100000;
        setTick(delta >= 0 ? 'up' : 'down');
        return next;
      });
    }, 1800);
    return () => clearInterval(id);
  }, []);
  return { price, tick };
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

export default function Header({ selectedPair }) {
  const [now, setNow] = useState(new Date());
  const { price, tick } = useSimulatedPrice(currentSignal.price);
  const { status, error: statusError } = useDataStatus();
  const pairLabel = selectedPair?.label ?? 'EUR/USD';

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const utcTime = now.toUTCString().slice(17, 25);
  const session = getCurrentSession();
  const isUp = currentSignal.changePct >= 0;

  const isLive    = status?.dataMode === 'live';
  const provider  = status?.fxProvider ?? null;
  const lastSeen  = status?.timestamp
    ? new Date(status.timestamp).toLocaleTimeString()
    : null;

  return (
    <header className="bg-[#0d1117] border-b border-[#263044] px-4 py-3 flex items-center justify-between flex-wrap gap-3">

      {/* Logo / Title */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center">
            <Activity size={14} className="text-white" />
          </div>
          <span className="text-sm font-semibold text-white tracking-wide">FX Signal Pro</span>
        </div>
        <span className="text-[#263044] hidden sm:block">|</span>
        <span className="text-xs text-gray-500 hidden sm:block">EUR/USD Intelligence Dashboard</span>
      </div>

      {/* Live Price Ticker */}
      <div className="flex items-center gap-2 bg-[#161b27] border border-[#263044] rounded-lg px-4 py-2">
        <span className="text-xs font-semibold text-gray-400 mr-1">{pairLabel}</span>
        <span
          className={`font-mono text-xl font-bold transition-colors duration-300 ${
            tick === 'up' ? 'text-emerald-400' : tick === 'down' ? 'text-red-400' : 'text-white'
          }`}
        >
          {price.toFixed(5)}
        </span>
        <div className={`flex items-center gap-0.5 text-xs font-mono ${isUp ? 'text-emerald-400' : 'text-red-400'}`}>
          {isUp ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
          {isUp ? '+' : ''}{currentSignal.changePct.toFixed(2)}%
        </div>
      </div>

      {/* Status Bar */}
      <div className="flex items-center gap-3 text-xs text-gray-500 flex-wrap">

        {/* Session */}
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-gray-300">{session}</span>
        </div>

        {/* UTC Clock */}
        <div className="flex items-center gap-1.5 hidden sm:flex">
          <Clock size={12} />
          <span className="font-mono">{utcTime} UTC</span>
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
