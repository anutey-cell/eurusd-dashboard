import { useState, useEffect, useCallback, useRef } from 'react';
import { RefreshCw, Clock, Shield, Zap } from 'lucide-react';

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
import MT5Panel               from './MT5Panel';
import EngineMaturityCard     from './EngineMaturityCard';
import InstitutionalScanPanel from './InstitutionalScanPanel';
import ScanHistoryPanel       from './ScanHistoryPanel';
import XauusdBacktestPanel    from './XauusdBacktestPanel';
import PaperObservationPanel  from './PaperObservationPanel';
import HighProbabilityPanel   from './HighProbabilityPanel';
import KillzonePanel          from './KillzonePanel';
import AutonomousExecutorPanel from './AutonomousExecutorPanel';
import ProbabilitySweepPanel  from './ProbabilitySweepPanel';
import EngineDiagnosticsPanel from './EngineDiagnosticsPanel';
import EngineComparisonPanel  from './EngineComparisonPanel';
import TraderMindsetPanel     from './TraderMindsetPanel';
import TraderDevelopmentPanel from './TraderDevelopmentPanel';
import IntermarketCorrelationPanel from './IntermarketCorrelationPanel';
import InstitutionalStrategistPanel from './InstitutionalStrategistPanel';
import VpTrapPanel                   from './VpTrapPanel';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';

// ─── Tab system — groups 18+ panels into 5 task-oriented tabs ───────────────
const TABS = [
  { id: 'live',        label: 'Live'        },
  { id: 'context',     label: 'Context'     },
  { id: 'learning',    label: 'Learning'    },
  { id: 'backtest',    label: 'Backtest'    },
  { id: 'journal',     label: 'Journal'     },
];

// ─── Instrument ───────────────────────────────────────────────────────────────
// XAU/USD is the only supported instrument.
const INSTRUMENT = { code: 'xauusd', label: 'XAU/USD', symbol: 'OANDA:XAUUSD', decimals: 2 };

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

export default function Dashboard({ instrument = INSTRUMENT }) {
  // Signal fetch for XAU/USD only
  const signal    = useDataSource(
    () => getSignalForPair(instrument.code),
    FALLBACKS.signal,
    REFRESH_MS,
  );
  const calendar  = useDataSource(getMacroCalendar, FALLBACKS.calendar,  REFRESH_MS);
  const history   = useDataSource(getSignalHistory, FALLBACKS.history,   null);
  const analytics = useDataSource(getAnalytics,     FALLBACKS.analytics, null);

  // Tab selection — persisted to localStorage so reloads keep your view
  const [activeTab, setActiveTab] = useState(() => {
    try { return localStorage.getItem('xau_tab') || 'live'; }
    catch { return 'live'; }
  });
  useEffect(() => {
    try { localStorage.setItem('xau_tab', activeTab); } catch {}
  }, [activeTab]);

  // journalKey bumps each time a trade is confirmed → triggers PaperTradeJournal reload
  const [journalKey, setJournalKey] = useState(0);
  // scanKey bumps each time a forced scan completes → triggers ScanHistoryPanel reload
  const [scanKey, setScanKey] = useState(0);

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
                ? 'Refreshing XAU/USD signal & calendar…'
                : <>Signal &amp; Calendar refresh in <span className="font-mono text-gray-400">{countdown}s</span></>}
            </span>
          </div>
          {lastUpdated && (
            <div className="flex items-center gap-1 hidden sm:flex">
              <Clock size={10} />
              <span>
                Last update: <span className="font-mono text-gray-400">{formatKenyaTime(lastUpdated)} {KENYA_LABEL}</span>
              </span>
            </div>
          )}
        </div>
        <div className="flex items-center gap-3">
          {/* Instrument badge — no pair selector, XAU/USD only */}
          <div className="flex items-center gap-1.5 bg-[#131c27] border border-amber-500/30 rounded-lg px-3 py-1.5">
            <Zap size={10} className="text-amber-400" />
            <span className="text-xs font-semibold text-amber-300">XAU/USD</span>
            <span className="text-[10px] text-gray-500 ml-1">Gold · 50 pts target</span>
          </div>
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
          XAU/USD · Manual confirmation mode · 50-point target · 1.0 point = $1 move ·
          Broker execution disabled · MONITOR_ONLY
        </span>
      </div>

      {/* ── Main layout ────────────────────────────────────────────────────── */}
      <main className="flex-1 p-4 space-y-4 max-w-[1920px] mx-auto w-full">

        {/* Tab navigation — persists in localStorage */}
        <div className="flex items-center gap-1 bg-[#0d1117] border border-[#263044] rounded-lg p-1 sticky top-2 z-10 shadow-lg">
          {TABS.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-1.5 text-xs font-bold uppercase tracking-widest rounded transition-colors ${
                activeTab === tab.id
                  ? 'bg-blue-500/30 text-blue-200 border border-blue-500/60'
                  : 'text-gray-400 hover:text-white hover:bg-[#263044]'
              }`}
            >
              {tab.label}
            </button>
          ))}
          <div className="ml-auto text-[10px] text-gray-500 px-2 hidden md:block">
            Live = signals · Context = macro · Learning = curriculum · Backtest = research · Journal = history
          </div>
        </div>

        {/* Row 1: three-column (always visible — anchor) */}
        <div className="flex gap-4 items-start">

          {/* Left — signal feeds both cards from one fetch */}
          <div className="flex flex-col gap-4 w-[320px] flex-shrink-0">
            <SignalCard
              loading={signal.loading}
              error={signal.error}
              isUsingFallback={signal.isUsingFallback}
              currentSignal={signal.data?.currentSignal}
              pairLabel="XAU/USD"
            />
            <TradePlanCard
              loading={signal.loading}
              error={signal.error}
              isUsingFallback={signal.isUsingFallback}
              tradePlan={signal.data?.tradePlan}
              decimals={2}
              targetLabel="points"
            />
          </div>

          {/* Center — TradingView chart for XAU/USD */}
          <div className="flex-1 min-w-0 flex flex-col gap-4">
            <div style={{ height: 540 }}>
              <TradingViewWidget symbol={instrument.symbol} />
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
              provider={calendar.data?.source}
            />
          </div>
        </div>

        {/* ═════════════════════════════════════════════════════════════════
            TAB CONTENT — only the active tab renders. Other tabs are
            unmounted, so their poll loops aren't burning CPU.
            ═══════════════════════════════════════════════════════════════ */}

        {activeTab === 'live' && (
          <>
            {/* ★ Headline card — institutional strategist verdict */}
            <InstitutionalStrategistPanel />
            {/* 🪤 VP Trap strategy — prev-day profile + trapped traders */}
            <VpTrapPanel />
            {/* Pre-trade checklist (local) */}
            <PreTradeChecklist />
            {/* Engine diagnostics — why no signal RIGHT NOW */}
            <EngineDiagnosticsPanel />
            {/* Killzone edge analyser — current session posture */}
            <KillzonePanel />
            {/* Autonomous executor — what the live system is about to do */}
            <AutonomousExecutorPanel />
            {/* High-probability predictor — multi-layer confluence */}
            <HighProbabilityPanel />
            {/* Institutional scanner — current SIGNAL_READY / SETUP_FORMING */}
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <InstitutionalScanPanel onScanComplete={() => setScanKey(k => k + 1)} />
              <ScanHistoryPanel refreshKey={scanKey} />
            </div>
          </>
        )}

        {activeTab === 'context' && (
          <>
            {/* Intermarket correlations — DXY/Yields/Oil/VIX */}
            <IntermarketCorrelationPanel />
            {/* Reuse News panel here too (also visible at top) — extended view */}
            <NewsPanel
              loading={calendar.loading}
              error={calendar.error}
              isUsingFallback={calendar.isUsingFallback}
              refetch={calendar.refetch}
              newsItems={calendar.data?.newsItems}
              recentNews={calendar.data?.recentNews}
              provider={calendar.data?.source}
            />
          </>
        )}

        {activeTab === 'learning' && (
          <>
            {/* Trader Mindset — 10-dim scorecard (band + total) */}
            <TraderMindsetPanel />
            {/* Trader Development — 20-principle curriculum from canonical books */}
            <TraderDevelopmentPanel />
            {/* Engine maturity — adaptive-weight self-assessment */}
            <EngineMaturityCard />
          </>
        )}

        {activeTab === 'backtest' && (
          <>
            {/* Probability sweep — 12-24 combos in one run */}
            <ProbabilitySweepPanel />
            {/* Engine comparison — swing vs intraday vs momentum */}
            <EngineComparisonPanel />
            {/* Strict XAU/USD backtest (primary) */}
            <XauusdBacktestPanel />
            {/* Legacy walk-forward backtest (kept for reference) */}
            <BacktestDashboard />
          </>
        )}

        {activeTab === 'journal' && (
          <>
            {/* Paper-trade panel — confirm + log */}
            <PaperTradePanel
              onConfirmed={() => setJournalKey(k => k + 1)}
              selectedPair={instrument}
            />
            {/* DB-backed paper trade journal */}
            <PaperTradeJournal refreshKey={journalKey} selectedPair={instrument} />
            {/* Paper observation tracker (dual + 3rd engine) */}
            <PaperObservationPanel />
            {/* MT5 demo panel */}
            <MT5Panel
              selectedPair={instrument}
              currentSignal={signal.data?.currentSignal}
              tradePlan={signal.data?.tradePlan}
            />
            {/* Signal history */}
            <SignalHistory
              loading={history.loading}
              error={history.error}
              isUsingFallback={history.isUsingFallback}
              refetch={history.refetch}
              trades={history.data?.trades}
            />
            {/* Broker execution (disabled panel for audit) */}
            <ExecutionPanel />
          </>
        )}
      </main>
    </div>
  );
}
