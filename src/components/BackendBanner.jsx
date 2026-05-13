import { useState, useEffect, useCallback } from 'react';
import { WifiOff, RefreshCw, AlertTriangle } from 'lucide-react';
import { getHealth } from '../services/api';

const POLL_INTERVAL_MS = 30_000;

export default function BackendBanner() {
  const [status, setStatus] = useState('checking'); // 'ok' | 'error' | 'checking'
  const [detail, setDetail]  = useState('');
  const [retrying, setRetrying] = useState(false);

  const check = useCallback(async (manual = false) => {
    if (manual) setRetrying(true);
    setStatus('checking');
    try {
      const res = await getHealth();
      const provider = res.data_mode === 'demo' ? 'demo' : (res.fx_provider ?? res.data_mode);
      setDetail(`v${res.version} · ${provider}`);
      setStatus('ok');
    } catch (err) {
      setDetail(err.message || 'Cannot reach backend');
      setStatus('error');
    } finally {
      if (manual) setRetrying(false);
    }
  }, []);

  useEffect(() => {
    check();
    const id = setInterval(check, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [check]);

  // Only render the banner when not OK
  if (status === 'ok') return null;

  return (
    <div
      className={`flex items-center gap-3 px-4 py-2 text-xs font-medium border-b transition-colors ${
        status === 'checking'
          ? 'bg-blue-500/10 border-blue-500/20 text-blue-300'
          : 'bg-amber-500/10 border-amber-500/20 text-amber-300'
      }`}
    >
      {status === 'checking' ? (
        <RefreshCw size={13} className="animate-spin flex-shrink-0" />
      ) : (
        <WifiOff size={13} className="flex-shrink-0 text-amber-400" />
      )}

      <span className="flex-1">
        {status === 'checking'
          ? 'Connecting to backend…'
          : `Backend offline — showing mock data. ${detail}`}
      </span>

      {status === 'error' && (
        <button
          onClick={() => check(true)}
          disabled={retrying}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded border border-amber-500/30 hover:bg-amber-500/10 transition-colors disabled:opacity-50"
        >
          <RefreshCw size={11} className={retrying ? 'animate-spin' : ''} />
          Retry
        </button>
      )}
    </div>
  );
}

// Inline status chip for individual components that use fallback data
export function FallbackChip({ isUsingFallback, error }) {
  if (!isUsingFallback) return null;
  return (
    <span
      title={error}
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/20"
    >
      <AlertTriangle size={9} />
      MOCK
    </span>
  );
}
