import { useState, useEffect, useCallback, useRef } from 'react';
import { RefreshCw, Clock, Shield } from 'lucide-react';

import { getSignalForPair, getMacroCalendar, getSignalHistory, getAnalytics } from '../services/api';
import {
  currentSignal  as mockSignal,
  tradePlan      as mockTradePlan,
  newsItems      as mockNewsItems,
  recentNews     as mockRecentNews,
  signalHistory  as mockHistory,
  mockFullAnalytics,
} from '../data/mockData';

import SignalCard          from './SignalCard';
import TradePlanCard       from './TradePlanCard';
import TradingViewWidget   from './TradingViewWidget';
import InstitutionalPanel  from './InstitutionalPanel';
import NewsPanel           from './NewsPanel';
import PreTradeChecklist   from './PreTradeChecklist';
import SignalHistory       from './SignalHistory';
import AnalyticsDashboard  from './AnalyticsDashboard';
import BacktestDashboard   from './BacktestDashboard';
import ExecutionPanel      from './ExecutionPanel';
import PaperTradePanel     from './PaperTradePanel';
import PaperTradeJournal   from './PaperTradeJournal';
import MT5Panel            from './MT5Panel';
import EngineMaturityCard  from './EngineMaturityCard';

// ─── Pair options ────────────────────────────────────────────────────────────

const PAIR_OPTIONS = [
  { code: 'eurusd', label: 'EUR/USD', symbol: 'FX:EURUSD',      decimals: 5 },
  { code: 'xauusd', label: 'XAU/USD', symbol: 'TVC:GOLD',       decimals: 2 },
];

function PairSelector({ selected, onChange }) {
  return (
    <div className="flex items-center gap-1 bg-[#131c27] border border-[#263044] rounded-lg p-0.5">
      {PAIR_OPTIONS.map(p => (
        <button
          key={p.code}
          onClick={() => onChange(p)}
          className={`px-3 py-1.5 rounded-md text-xs font-semibold transition-colors ${
            selected === p.code
              ? 'bg-blue-600 text-white'
              : 'text-gray-500 hover:text-gray-300'
          }`}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}

// ─── Constants ────────────────────────────────────────────────────────────────

const REFRESH_MS = 60_000;

const FALLBACKS = {
  signal: {
    currentSignal: mockSignal,
    tradePlan:     mockTradePlan,
  },
  calendar: {
    newsItems:  mockNewsItems,
    recentNews: mockRecentNews,
  },
  history: {
    trades:   mockHistory,
    total:    mockHistory.length,
    page:     1,
    pageSize: 20,
  },
  analytics: mockFullAnalytics,
};

// ─── useDataSource ────────────────────────────────────────────────────────────
// Generic fetch hook with optional auto-refresh.
// silent=true (used by the interval) skips the loading spinner so the UI
// doesn't flicker every 60 s — the data just updates quietly.

function useDataSource(fetchFn, fallback, refreshMs = null) {
  const fnRef = useRef(fetchFn);
  fnRef.current = fetchFn;

  const [state, setState] = useState({
    data:            fallback,
    loading:         true,
    error:           null,
    isUsingFallback: false,
    lastUpdated:     null,
  });

  const run = useCallback(async (silent = false) => {
    if (!silent) setState(prev => ({ ...prev, loading: true, error: null }));
    try {
      const data = await fnRef.current();
      setState({ data, loading: false, error: null, isUsingFallback: false, lastUpdated: new Date() });
    } catch (err) {
      setState(prev => ({
        ...prev,
        data:            fallback,
        loading:         false,
        error:           err?.message ?? 'Request failed',
        isUsingFallback: true,
        lastUpdated:     null,
      }));
    }
  // fallback is a module-level constant; fnRef keeps fetchFn current without
  // being a dependency, so run() is intentionally stable.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    run();
    if (!refreshMs) return;
    const id = setInterval(() => run(true), refreshMs);
    return () => clearInterval(id);
  }, [run, refreshMs]);

  return { ...state, refetch: () => run(false) };
}

// ─── Countdown ticker ─────────────────────────────────────────────────────────

function useRefreshCountdown(ms) {
  const total = ms / 1000;
  const [secs, setSecs] = useState(total);
  useEffect(() => {
    setSecs(total);
    const id = setInterval(() => setSecs(prev => (prev <= 1 ? total : prev - 1)), 1000);
    return () => clearInterval(id);
  }, [total]);
  return secs;
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

export default function Dashboard({ selectedPair, onPairChange }) {
  // Signal fetch is pair-aware — re-fires whenever selectedPair changes
  const signal    = useDataSource(
    () => getSignalForPair(selectedPair.code),
    FALLBACKS.signal,
    REFRESH_MS,
  );
  const calendar  = useDataSource(getMacroCalendar, FALLBACKS.calendar,  REFRESH_MS);
  const history   = useDataSource(getSignalHistory, FALLBACKS.history,   null);
  const analytics = useDataSource(getAnalytics,     FALLBACKS.analytics, null);

  // Re-fetch signal immediately when the active pair changes
  // (useDataSource keeps fnRef current but won't auto-fire on prop change)
  useEffect(() => {
    signal.refetch();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPair.code]);

  // journalKey bumps each time a trade is confirmed → triggers PaperTradeJournal reload
  const [journalKey, setJournalKey] = useState(0);

  // selectedPair / onPairChange lifted to App.jsx so Header can also read it
  const setSelectedPair = onPairChange ?? (() => {});

  const countdown    = useRefreshCountdown(REFRESH_MS);
  const refreshing   = signal.loading || calendar.loading;
  const anyFallback  = signal.isUsingFallback || calendar.isUsingFallback
                    || history.isUsingFallback || analytics.isUsingFallback;

  const lastUpdated = signal.lastUpdated;

  return (
    <div className="flex-1 flex flex-col min-h-0">

      {/* ── Refresh status bar ─────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-4 py-1.5 border-b border-[#263044] bg-[#0d1117]">
        <div className="flex items-center gap-4 text-[10px] text-gray-600">
          <div className="flex items-center gap-1.5">
            <RefreshCw
              size={10}
              className={refreshing ? 'animate-spin text-blue-400' : 'text-gray-700'}
            />
            <span>
              {refreshing
                ? 'Refreshing signal & calendar…'
                : <>Signal &amp; Calendar refresh in <span className="font-mono text-gray-400">{countdown}s</span></>}
            </span>
          </div>
          {lastUpdated && (
            <div className="flex items-center gap-1 hidden sm:flex">
              <Clock size={10} />
              <span>
                Last update: <span className="font-mono text-gray-400">{lastUpdated.toLocaleTimeString()}</span>
              </span>
            </div>
          )}
        </div>
        <div className="flex items-center gap-3">
          <PairSelector selected={selectedPair.code} onChange={setSelectedPair} />
          {anyFallback && (
            <span className="text-[10px] text-amber-400/60 italic">
              One or more panels using mock fallback
            </span>
          )}
        </div>
      </div>

      {/* ── Safety Banner ──────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 px-4 py-1.5 bg-amber-500/5 border-b border-amber-500/10 text-[10px] text-amber-400/70">
        <Shield size={10} className="flex-shrink-0" />
        <span>
          Manual confirmation mode — this system does not execute trades.
          {selectedPair.code === 'xauusd' && ' XAU/USD uses 1.0 point = 1 USD move.'}
        </span>
      </div>

      {/* ── Main layout ────────────────────────────────────────────────────── */}
      <main className="flex-1 p-4 space-y-4 max-w-[1920px] mx-auto w-full">

        {/* Row 1: three-column */}
        <div className="flex gap-4 items-start">

          {/* Left — signal feeds both cards from one fetch */}
          <div className="flex flex-col gap-4 w-[320px] flex-shrink-0">
            <SignalCard
              loading={signal.loading}
              error={signal.error}
              isUsingFallback={signal.isUsingFallback}
              currentSignal={signal.data?.currentSignal}
              pairLabel={selectedPair.label}
            />
            <TradePlanCard
              loading={signal.loading}
              error={signal.error}
              isUsingFallback={signal.isUsingFallback}
              tradePlan={signal.data?.tradePlan}
              decimals={selectedPair.decimals}
            />
          </div>

          {/* Center — chart stays frontend-only */}
          <div className="flex-1 min-w-0 flex flex-col gap-4">
            <div style={{ height: 540 }}>
              <TradingViewWidget key={selectedPair.symbol} symbol={selectedPair.symbol} />
            </div>
            <AnalyticsDashboard
              loading={analytics.loading}
              error={analytics.error}
              isUsingFallback={analytics.isUsingFallback}
              refetch={analytics.refetch}
              analytics={analytics.data}
            />
          </div>

          {/* Right */}
          <div className="flex flex-col gap-4 w-[320px] flex-shrink-0">
            <InstitutionalPanel />
            <NewsPanel
              loading={calendar.loading}
              error={calendar.error}
              isUsingFallback={calendar.isUsingFallback}
              refetch={calendar.refetch}
              newsItems={calendar.data?.newsItems}
              recentNews={calendar.data?.recentNews}
            />
          </div>
        </div>

        {/* Row 2: checklist (local state only — no API) */}
        <PreTradeChecklist />

        {/* Row 3: adaptive learning engine self-assessment */}
        <EngineMaturityCard />

        {/* Row 4: paper trading — run analysis + confirm */}
        <PaperTradePanel
          onConfirmed={() => setJournalKey(k => k + 1)}
          selectedPair={selectedPair}
        />

        {/* Row 4: paper trading journal — DB-backed history with Log Result */}
        <PaperTradeJournal refreshKey={journalKey} selectedPair={selectedPair} />

        {/* Row: MT5 Demo Panel — self-contained, manages its own data fetching */}
        <MT5Panel
          selectedPair={selectedPair}
          currentSignal={signal.data?.currentSignal}
          tradePlan={signal.data?.tradePlan}
        />

        {/* Row 5: mock signal history */}
        <SignalHistory
          loading={history.loading}
          error={history.error}
          isUsingFallback={history.isUsingFallback}
          refetch={history.refetch}
          trades={history.data?.trades}
        />

        {/* Row 4: backtesting */}
        <BacktestDashboard />

        {/* Row 5: broker execution (disabled panel) */}
        <ExecutionPanel />
      </main>
    </div>
  );
}
