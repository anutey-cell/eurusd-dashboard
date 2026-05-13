// ─── Current Signal ───────────────────────────────────────────────────────────
export const currentSignal = {
  direction: 'BUY',
  strength: 78,
  confidence: 72,
  timestamp: '2026-05-13T08:42:00Z',
  session: 'London',
  timeframe: 'H4',
  price: 1.08432,
  change: +0.00087,
  changePct: +0.08,
  factors: [
    { name: 'Trend Alignment',   value: 'Bullish (H4/D1)',     score: 85, positive: true  },
    { name: 'Market Structure',  value: 'Higher Highs / HLs',  score: 80, positive: true  },
    { name: 'RSI (H4)',          value: '58.4 — Bullish Zone', score: 68, positive: true  },
    { name: 'MACD',              value: 'Bullish Cross',        score: 72, positive: true  },
    { name: 'Volume Delta',      value: 'Accumulation +2.8M',  score: 70, positive: true  },
    { name: 'DXY Correlation',   value: 'Weakening (bearish)',  score: 62, positive: true  },
    { name: 'Spread',            value: '1.2 pips (normal)',    score: 90, positive: true  },
    { name: 'Session Overlap',   value: 'London Open Active',  score: 75, positive: true  },
  ],
};

// ─── Trade Plan ───────────────────────────────────────────────────────────────
export const tradePlan = {
  entry: 1.08450,
  stopLoss: 1.08100,
  stopLossPips: 35,
  targets: [
    { label: 'TP1', price: 1.08800, rr: '1.0', pips: 35,  partial: '50%' },
    { label: 'TP2', price: 1.09100, rr: '1.86', pips: 65, partial: '30%' },
    { label: 'TP3', price: 1.09450, rr: '2.86', pips: 100, partial: '20%' },
  ],
  riskPercent: 1.5,
  positionSize: 0.75,
  accountSize: 10000,
  riskAmount: 150,
  validity: '2026-05-13T20:00:00Z',
  notes: 'Wait for London close H1 candle confirmation above 1.0840. Entry on pullback to the 1.0840–1.0845 demand zone. Invalidated on close below 1.0810.',
};

// ─── Institutional Data ───────────────────────────────────────────────────────
export const institutionalData = {
  cotData: {
    netPositions: +52340,
    weeklyChange: +8120,
    sentiment: 'Bullish',
    commercials: -45200,
    largeSpreaders: +52340,
    smallSpeculators: -7140,
    reportDate: '2026-05-06',
  },
  orderFlow: {
    imbalance: 'Buy',
    buyRatio: 62,
    delta: +2840000,
    absorption: 'High',
    poc: 1.08280,
    vah: 1.08720,
    val: 1.07950,
  },
  darkPoolActivity: {
    level: 'Elevated',
    prints: 14,
    avgSize: 2500000,
    trend: 'Accumulating',
    lastPrint: '08:31 UTC',
  },
  bankLevels: [
    { bank: 'Goldman Sachs', level: 1.09000, type: 'Offer', size: 'Large',  confidence: 90 },
    { bank: 'JP Morgan',     level: 1.08780, type: 'Offer', size: 'Medium', confidence: 75 },
    { bank: 'Deutsche Bank', level: 1.08400, type: 'Bid',   size: 'Large',  confidence: 85 },
    { bank: 'Citi',          level: 1.08050, type: 'Bid',   size: 'Medium', confidence: 70 },
    { bank: 'Barclays',      level: 1.07800, type: 'Bid',   size: 'Small',  confidence: 60 },
  ],
};

// ─── News / Economic Calendar ─────────────────────────────────────────────────
export const newsItems = [
  {
    id: 1,
    time: '2026-05-13T10:00:00Z',
    currency: 'USD',
    event: 'Core CPI m/m',
    impact: 'high',
    forecast: '0.3%',
    previous: '0.4%',
    actual: null,
    pending: true,
  },
  {
    id: 2,
    time: '2026-05-13T12:30:00Z',
    currency: 'USD',
    event: 'FOMC Member Speech',
    impact: 'medium',
    forecast: '—',
    previous: '—',
    actual: null,
    pending: true,
  },
  {
    id: 3,
    time: '2026-05-13T07:00:00Z',
    currency: 'EUR',
    event: 'German ZEW Economic Sentiment',
    impact: 'medium',
    forecast: '12.3',
    previous: '11.9',
    actual: '13.1',
    pending: false,
    beat: true,
  },
  {
    id: 4,
    time: '2026-05-13T06:00:00Z',
    currency: 'EUR',
    event: 'French Industrial Output m/m',
    impact: 'low',
    forecast: '0.2%',
    previous: '-0.1%',
    actual: '0.4%',
    pending: false,
    beat: true,
  },
  {
    id: 5,
    time: '2026-05-13T14:00:00Z',
    currency: 'USD',
    event: 'Michigan Consumer Sentiment',
    impact: 'medium',
    forecast: '76.5',
    previous: '75.1',
    actual: null,
    pending: true,
  },
];

export const recentNews = [
  {
    id: 1,
    time: '08:15',
    headline: 'ECB Lagarde signals data-dependent pause; EUR rallies on reduced cut expectations',
    source: 'Reuters',
    sentiment: 'bullish',
  },
  {
    id: 2,
    time: '07:44',
    headline: 'US Treasury yields dip ahead of CPI print; DXY softens toward 104.20',
    source: 'Bloomberg',
    sentiment: 'bullish',
  },
  {
    id: 3,
    time: '07:22',
    headline: 'German ZEW beats estimates at 13.1 vs 12.3 forecast, boosting EUR sentiment',
    source: 'ForexFactory',
    sentiment: 'bullish',
  },
  {
    id: 4,
    time: '06:50',
    headline: 'Fed officials push back on near-term cut expectations amid sticky services inflation',
    source: 'WSJ',
    sentiment: 'bearish',
  },
  {
    id: 5,
    time: '06:12',
    headline: 'EUR/USD tests key 1.0850 resistance — break opens path to 1.0920',
    source: 'FXStreet',
    sentiment: 'bullish',
  },
];

// ─── Pre-Trade Checklist ───────────────────────────────────────────────────────
export const checklistCategories = [
  {
    id: 'trend',
    label: 'Trend & Structure',
    required: true,
    items: [
      { id: 'c1', label: 'D1 trend is clearly bullish (HH/HL)', required: true,  defaultChecked: true  },
      { id: 'c2', label: 'H4 trend aligns with D1 direction',    required: true,  defaultChecked: true  },
      { id: 'c3', label: 'Price above key moving averages (50/200 EMA)', required: false, defaultChecked: true },
      { id: 'c4', label: 'No major structure level directly overhead', required: true,  defaultChecked: false },
    ],
  },
  {
    id: 'confirmation',
    label: 'Entry Confirmation',
    required: true,
    items: [
      { id: 'c5', label: 'H1 pullback to identified demand zone',         required: true,  defaultChecked: true  },
      { id: 'c6', label: 'Bullish rejection candle or engulfing pattern', required: true,  defaultChecked: false },
      { id: 'c7', label: 'RSI divergence or oversold signal on H1',       required: false, defaultChecked: true  },
      { id: 'c8', label: 'Volume confirms buying pressure at entry zone',  required: false, defaultChecked: true  },
    ],
  },
  {
    id: 'risk',
    label: 'Risk Management',
    required: true,
    items: [
      { id: 'c9',  label: 'Risk is ≤1.5% of account balance',  required: true, defaultChecked: true  },
      { id: 'c10', label: 'R:R ratio ≥ 1.5 to nearest TP',     required: true, defaultChecked: true  },
      { id: 'c11', label: 'Stop placed beyond structure (not round number)', required: true, defaultChecked: true },
      { id: 'c12', label: 'No correlated positions open',       required: true, defaultChecked: true  },
    ],
  },
  {
    id: 'session',
    label: 'Session & News',
    required: false,
    items: [
      { id: 'c13', label: 'No high-impact news within 30 minutes of entry', required: true,  defaultChecked: false },
      { id: 'c14', label: 'Trading within active session (London/NY)',       required: false, defaultChecked: true  },
      { id: 'c15', label: 'Spread is within acceptable range (<2 pips)',     required: false, defaultChecked: true  },
    ],
  },
];

// ─── Signal History ───────────────────────────────────────────────────────────
export const signalHistory = [
  { id: 1,  date: '2026-05-12', time: '09:15', dir: 'BUY',  entry: 1.07820, exit: 1.08210, pips: +39,  rr: 1.95, result: 'WIN',     pnl: +195, confidence: 74 },
  { id: 2,  date: '2026-05-12', time: '14:30', dir: 'SELL', entry: 1.08350, exit: 1.08100, pips: +25,  rr: 1.25, result: 'WIN',     pnl: +125, confidence: 65 },
  { id: 3,  date: '2026-05-11', time: '10:00', dir: 'BUY',  entry: 1.07640, exit: 1.07410, pips: -23,  rr: -1.0, result: 'LOSS',    pnl: -150, confidence: 58 },
  { id: 4,  date: '2026-05-09', time: '08:45', dir: 'BUY',  entry: 1.07300, exit: 1.07820, pips: +52,  rr: 2.60, result: 'WIN',     pnl: +260, confidence: 82 },
  { id: 5,  date: '2026-05-08', time: '13:15', dir: 'SELL', entry: 1.08100, exit: 1.08100, pips:   0,  rr:  0.0, result: 'BREAKEVEN', pnl: 0,   confidence: 61 },
  { id: 6,  date: '2026-05-07', time: '09:00', dir: 'SELL', entry: 1.08640, exit: 1.08290, pips: +35,  rr: 1.75, result: 'WIN',     pnl: +175, confidence: 77 },
  { id: 7,  date: '2026-05-06', time: '10:30', dir: 'BUY',  entry: 1.07980, exit: 1.07760, pips: -22,  rr: -1.0, result: 'LOSS',    pnl: -150, confidence: 60 },
  { id: 8,  date: '2026-05-05', time: '08:15', dir: 'BUY',  entry: 1.07500, exit: 1.08120, pips: +62,  rr: 3.10, result: 'WIN',     pnl: +310, confidence: 88 },
  { id: 9,  date: '2026-05-02', time: '14:00', dir: 'SELL', entry: 1.08900, exit: 1.08560, pips: +34,  rr: 1.70, result: 'WIN',     pnl: +170, confidence: 73 },
  { id: 10, date: '2026-05-01', time: '09:45', dir: 'BUY',  entry: 1.07150, exit: 1.07420, pips: +27,  rr: 1.35, result: 'WIN',     pnl: +135, confidence: 69 },
  { id: 11, date: '2026-04-30', time: '11:00', dir: 'SELL', entry: 1.08450, exit: 1.08700, pips: -25,  rr: -1.0, result: 'LOSS',    pnl: -150, confidence: 55 },
  { id: 12, date: '2026-04-29', time: '08:30', dir: 'BUY',  entry: 1.07640, exit: 1.08190, pips: +55,  rr: 2.75, result: 'WIN',     pnl: +275, confidence: 85 },
  { id: 13, date: '2026-04-28', time: '13:30', dir: 'SELL', entry: 1.08820, exit: 1.08470, pips: +35,  rr: 1.75, result: 'WIN',     pnl: +175, confidence: 76 },
  { id: 14, date: '2026-04-25', time: '09:00', dir: 'BUY',  entry: 1.07200, exit: 1.07000, pips: -20,  rr: -1.0, result: 'LOSS',    pnl: -150, confidence: 52 },
  { id: 15, date: '2026-04-24', time: '10:15', dir: 'SELL', entry: 1.08300, exit: 1.07950, pips: +35,  rr: 1.75, result: 'WIN',     pnl: +175, confidence: 78 },
  { id: 16, date: '2026-04-23', time: '08:00', dir: 'BUY',  entry: 1.07050, exit: 1.07580, pips: +53,  rr: 2.65, result: 'WIN',     pnl: +265, confidence: 81 },
  { id: 17, date: '2026-04-22', time: '14:45', dir: 'SELL', entry: 1.08500, exit: 1.08250, pips: +25,  rr: 1.25, result: 'WIN',     pnl: +125, confidence: 67 },
  { id: 18, date: '2026-04-17', time: '09:30', dir: 'BUY',  entry: 1.06800, exit: 1.06560, pips: -24,  rr: -1.0, result: 'LOSS',    pnl: -150, confidence: 57 },
  { id: 19, date: '2026-04-16', time: '11:30', dir: 'SELL', entry: 1.08120, exit: 1.07780, pips: +34,  rr: 1.70, result: 'WIN',     pnl: +170, confidence: 72 },
  { id: 20, date: '2026-04-15', time: '08:00', dir: 'BUY',  entry: 1.06950, exit: 1.07480, pips: +53,  rr: 2.65, result: 'WIN',     pnl: +265, confidence: 83 },
];

// ─── Analytics Data ────────────────────────────────────────────────────────────
const seedEquityCurve = () => {
  const points = [];
  let equity = 10000;
  const start = new Date('2026-02-13');
  for (let i = 0; i < 90; i++) {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    if (d.getDay() === 0 || d.getDay() === 6) continue;
    const change = (Math.random() - 0.38) * 180;
    equity = Math.max(9200, equity + change);
    points.push({
      date: d.toISOString().slice(0, 10),
      equity: Math.round(equity * 100) / 100,
    });
  }
  // Force an upward trajectory for the mock
  let adj = 10000;
  return points.map((p, i) => {
    adj += (Math.random() - 0.35) * 160;
    adj = Math.max(9400, adj);
    return { ...p, equity: Math.round(adj * 100) / 100 };
  });
};

export const equityCurveData = seedEquityCurve();

export const monthlyPnlData = [
  { month: 'Nov', pnl: +620  },
  { month: 'Dec', pnl: -180  },
  { month: 'Jan', pnl: +1050 },
  { month: 'Feb', pnl: +430  },
  { month: 'Mar', pnl: +870  },
  { month: 'Apr', pnl: -220  },
  { month: 'May', pnl: +1320 },
  { month: 'Jun', pnl: +580  },
  { month: 'Jul', pnl: +240  },
  { month: 'Aug', pnl: -95   },
  { month: 'Sep', pnl: +760  },
  { month: 'Oct', pnl: +990  },
];

export const sessionPerformanceData = [
  { session: 'Tokyo',   winRate: 48, avgPips: 18, trades: 21 },
  { session: 'London',  winRate: 72, avgPips: 38, trades: 87 },
  { session: 'NY Open', winRate: 68, avgPips: 32, trades: 64 },
  { session: 'NY PM',   winRate: 54, avgPips: 14, trades: 29 },
];

export const analyticsKPIs = {
  winRate: 70,
  totalTrades: 201,
  totalPips: +1847,
  totalPnl: +3240,
  avgRR: 1.94,
  profitFactor: 2.31,
  maxDrawdown: -8.4,
  sharpeRatio: 1.72,
  avgWinPips: 41,
  avgLossPips: 23,
  expectancy: 18.4,
  bestMonth: 'Jan +$1,050',
  worstMonth: 'Dec -$180',
  currentStreak: '+5 wins',
};

// ─── Full analytics (Phase 7 shape) — computed from the 20-trade mock history ─
const _buildMockEquityCurve = () => {
  const pips = [39,25,-23,52,0,35,-22,62,34,27,-25,55,35,-20,35,53,25,-24,34,53];
  let cum = 0;
  return pips.map((p, i) => ({ trade: i + 1, pips: p, cumulativePips: (cum += p) }));
};

export const mockFullAnalytics = {
  sampleInfo: {
    size: 'low',
    completedTrades: 20,
    message: 'Low confidence — only 20 completed trades. Need 20+ for reliable statistics.',
  },
  totalSignals: 20,
  confirmedTrades: 20,
  completedTrades: 20,
  winRate: 70.0,
  lossRate: 25.0,
  averageWinPips: 40.3,
  averageLossPips: 22.8,
  averageRR: 2.05,
  averagePips: 22.5,
  expectancy: 22.5,
  profitFactor: 4.95,
  maxDrawdown: -25,
  consecutiveWins: 3,
  consecutiveLosses: 1,
  longWinRate: 63.6,
  shortWinRate: 77.8,
  newsDayWinRate: null,
  nonNewsDayWinRate: 70.0,
  bestSession: 'London',
  worstSession: null,
  sessionBreakdown: [
    { session: 'London',   trades: 14, wins: 10, winRate: 71.4, avgPips: 25.1 },
    { session: 'Overlap',  trades: 4,  wins: 3,  winRate: 75.0, avgPips: 23.5 },
    { session: 'New York', trades: 2,  wins: 1,  winRate: 50.0, avgPips: 5.5  },
  ],
  equityCurve: _buildMockEquityCurve(),
};
