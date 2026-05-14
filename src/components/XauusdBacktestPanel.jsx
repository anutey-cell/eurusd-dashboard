/**
 * XauusdBacktestPanel
 *
 * Strict XAU/USD backtest dashboard.
 * Sections (in render order):
 *   1. Settings form  — date range, balance, risk, costs, score, RR
 *   2. CSV import box — upload historical candles
 *   3. Run button + warnings banner
 *   4. Reliability rating card
 *   5. Summary metrics grid
 *   6. Equity curve + drawdown chart
 *   7. Breakdowns (session / direction / score band)
 *   8. Trade list (paginated)
 *   9. Skipped trades (collapsible)
 *
 * Pure analysis — never places trades, never sends Telegram alerts.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Cell, ComposedChart, Line,
} from 'recharts';
import {
  Play, AlertTriangle, Upload, Database, FlaskConical,
  TrendingUp, TrendingDown, Award, Shield, ChevronDown, ChevronUp,
  CheckCircle, XCircle, History,
} from 'lucide-react';
import {
  runXauusdBacktest, importBacktestCsv, getBacktestDataStatus, listBacktestRuns,
} from '../services/api';

// ── Constants ─────────────────────────────────────────────────────────────────

const TIMEFRAMES = ['M5', 'M15', 'M30', 'H1', 'H4', 'D1'];
const DEFAULTS = {
  timeframe:       'M15',
  lookback:        5000,
  initial_balance: 10000,
  risk_percent:    0.25,
  spread_points:   1.5,
  slippage_points: 0.5,
  min_score:       80,
  min_rr:          2.5,
  include_news_filter: true,
};

// ── Small helpers ─────────────────────────────────────────────────────────────

function Field({ label, children, hint }) {
  return (
    <label className="flex flex-col gap-1 min-w-0">
      <span className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">{label}</span>
      {children}
      {hint && <span className="text-[9px] text-gray-600">{hint}</span>}
    </label>
  );
}

function Input({ value, onChange, type = 'text', step, min, max }) {
  return (
    <input
      type={type}
      value={value}
      step={step}
      min={min}
      max={max}
      onChange={e => onChange(type === 'number' ? Number(e.target.value) : e.target.value)}
      className="bg-[#161b27] border border-[#263044] text-gray-200 text-xs rounded px-2 py-1.5 font-mono focus:outline-none focus:border-blue-500"
    />
  );
}

function Select({ value, onChange, options }) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className="bg-[#161b27] border border-[#263044] text-gray-200 text-xs rounded px-2 py-1.5 focus:outline-none focus:border-blue-500"
    >
      {options.map(o => <option key={o.value ?? o} value={o.value ?? o}>{o.label ?? o}</option>)}
    </select>
  );
}

function Stat({ label, value, sub, color = 'text-white', small = false }) {
  return (
    <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3 flex flex-col gap-0.5">
      <span className="text-[9px] text-gray-500 uppercase tracking-widest">{label}</span>
      <span className={`font-mono font-bold ${small ? 'text-sm' : 'text-base'} ${color}`}>{value}</span>
      {sub && <span className="text-[9px] text-gray-600">{sub}</span>}
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function ReliabilityCard({ reliability }) {
  if (!reliability) return null;
  const { score = 0, band = '', violations = [], verdict = '' } = reliability;

  const tier =
    score >= 85 ? { cls: 'from-emerald-700/40 to-emerald-900/20 border-emerald-600/50 text-emerald-300', label: 'STRONG'   } :
    score >= 70 ? { cls: 'from-blue-700/40    to-blue-900/20    border-blue-600/50    text-blue-300',    label: 'PROMISING' } :
    score >= 50 ? { cls: 'from-amber-700/40   to-amber-900/20   border-amber-600/50   text-amber-300',   label: 'WEAK'      } :
                  { cls: 'from-red-700/40     to-red-900/20     border-red-600/50     text-red-300',     label: 'INSUFFICIENT' };

  return (
    <div className={`bg-gradient-to-r ${tier.cls} border rounded-xl p-4 space-y-2`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Award size={15} />
          <span className="text-xs uppercase tracking-widest font-bold">Backtest Reliability</span>
        </div>
        <div className="text-right">
          <div className="text-3xl font-black font-mono leading-none">{score}<span className="text-sm opacity-50">/100</span></div>
          <div className="text-[10px] uppercase tracking-wider opacity-80">{tier.label}</div>
        </div>
      </div>
      <p className="text-[11px] opacity-90 leading-relaxed">{band}</p>
      <p className="text-[11px] opacity-80 italic">{verdict}</p>
      {violations.length > 0 && (
        <div className="pt-2 border-t border-current/20">
          <p className="text-[9px] uppercase tracking-wider opacity-70 mb-1">Violations:</p>
          {violations.map((v, i) => (
            <p key={i} className="text-[10px] font-mono opacity-90">- {v}</p>
          ))}
        </div>
      )}
    </div>
  );
}

function WarningsBanner({ warnings = [] }) {
  if (!warnings.length) return null;
  return (
    <div className="bg-amber-500/8 border border-amber-500/25 rounded-lg px-3 py-2.5 space-y-1">
      <div className="flex items-center gap-2 text-amber-300 text-[11px] font-semibold">
        <AlertTriangle size={12} /> Backtest is a filter, not proof
      </div>
      {warnings.map((w, i) => (
        <p key={i} className="text-[10px] text-amber-200/70 leading-tight">- {w}</p>
      ))}
    </div>
  );
}

function CsvImportBox({ onImported, timeframe }) {
  const [busy, setBusy]   = useState(false);
  const [msg,  setMsg]    = useState(null);
  const [err,  setErr]    = useState(null);
  const inputRef = useRef(null);

  async function handleFile(e) {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true); setMsg(null); setErr(null);
    try {
      const result = await importBacktestCsv(f, timeframe);
      setMsg(
        `Imported ${result.imported}, skipped ${result.skipped} ` +
        `(duplicates ${result.duplicates}, invalid ${result.invalid}) ` +
        `into ${result.timeframe}.`,
      );
      onImported?.(result);
    } catch (e) {
      setErr(e.message ?? 'Import failed');
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  return (
    <div className="bg-[#0a0f17] border border-[#1e2535] rounded-lg p-3 space-y-2">
      <div className="flex items-center gap-2 text-[10px] text-slate-500 uppercase tracking-widest font-semibold">
        <Upload size={11} /> Historical Candle Import (CSV)
      </div>
      <p className="text-[10px] text-gray-500 leading-tight">
        Columns: <span className="font-mono text-gray-400">timestamp,open,high,low,close[,volume]</span>.
        Timestamps must be ISO-8601 UTC. Duplicates skipped automatically.
      </p>
      <div className="flex items-center gap-2">
        <input
          ref={inputRef}
          type="file"
          accept=".csv,text/csv"
          disabled={busy}
          onChange={handleFile}
          className="text-[10px] text-gray-400 file:text-[10px] file:bg-blue-600 file:text-white file:border-0 file:rounded file:px-2 file:py-1 file:cursor-pointer file:mr-2"
        />
        <span className="text-[10px] text-gray-600">→ TF: <span className="font-mono">{timeframe}</span></span>
      </div>
      {busy && <p className="text-[10px] text-blue-400">Uploading…</p>}
      {msg && <p className="text-[10px] text-emerald-400">{msg}</p>}
      {err && <p className="text-[10px] text-red-400">{err}</p>}
    </div>
  );
}

function DataStatusPill({ status }) {
  if (!status) return null;
  if (!status.available) {
    return (
      <span className="text-[10px] text-amber-400/70 flex items-center gap-1">
        <Database size={10} /> No DB candles for {status.timeframe} — will use synthetic fallback
      </span>
    );
  }
  return (
    <span className="text-[10px] text-emerald-400/80 flex items-center gap-1">
      <Database size={10} /> {status.count.toLocaleString()} {status.timeframe} candles in DB
    </span>
  );
}

function EquityCurveChart({ curve = [], initialBalance }) {
  if (!curve.length) return null;
  const final = curve[curve.length - 1]?.equity ?? initialBalance;
  const positive = final >= initialBalance;
  const colour = positive ? '#10b981' : '#ef4444';

  return (
    <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-medium text-gray-300">Equity Curve</span>
        <span className={`text-xs font-mono ${positive ? 'text-emerald-400' : 'text-red-400'}`}>
          {positive ? '+' : ''}{((final - initialBalance) / initialBalance * 100).toFixed(2)}%
        </span>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <ComposedChart data={curve} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={colour} stopOpacity={0.3} />
              <stop offset="95%" stopColor={colour} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#263044" />
          <XAxis dataKey="step" tick={{ fill: '#6b7280', fontSize: 9 }} />
          <YAxis yAxisId="eq" tick={{ fill: '#6b7280', fontSize: 9 }} />
          <YAxis yAxisId="dd" orientation="right" tick={{ fill: '#9ca3af', fontSize: 9 }} unit="%" domain={[0, 'dataMax']} reversed />
          <Tooltip contentStyle={{ background: '#1a2035', border: '1px solid #263044', fontSize: 11 }} />
          <ReferenceLine yAxisId="eq" y={initialBalance} stroke="#475569" strokeDasharray="4 2" />
          <Area yAxisId="eq" type="monotone" dataKey="equity"
                stroke={colour} fill="url(#eqGrad)" strokeWidth={1.5} dot={false} name="Equity" />
          <Line yAxisId="dd" type="monotone" dataKey="drawdownPct"
                stroke="#f59e0b" strokeWidth={1} dot={false} name="Drawdown %" />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function BreakdownTable({ title, rows, cols }) {
  if (!rows?.length) return null;
  return (
    <div className="bg-[#161b27] border border-[#263044] rounded-lg p-3">
      <div className="text-xs font-medium text-gray-300 mb-2">{title}</div>
      <table className="w-full text-[10px]">
        <thead>
          <tr className="text-gray-500 uppercase tracking-wider">
            {cols.map(c => <th key={c.key} className="text-left py-1 px-1.5 border-b border-[#263044]">{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-[#1a2035]">
              {cols.map(c => (
                <td key={c.key} className={`py-1 px-1.5 font-mono ${c.cls?.(r) ?? 'text-gray-300'}`}>
                  {c.fmt ? c.fmt(r) : r[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SignalBadge({ signal }) {
  const cls =
    signal === 'BUY'  ? 'bg-emerald-500/20 text-emerald-400' :
    signal === 'SELL' ? 'bg-red-500/20 text-red-400' :
                        'bg-gray-500/20 text-gray-400';
  return <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded font-mono ${cls}`}>{signal}</span>;
}

function ResultBadge({ result }) {
  const cfg = {
    WIN:       'bg-emerald-500/20 text-emerald-400',
    LOSS:      'bg-red-500/20 text-red-400',
    BREAKEVEN: 'bg-amber-500/20 text-amber-400',
    EXPIRED:   'bg-purple-500/20 text-purple-400',
  }[result] ?? 'bg-gray-500/20 text-gray-400';
  return <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${cfg}`}>{result}</span>;
}

function TradeList({ trades = [] }) {
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState(false);
  const PAGE = 25;
  if (!trades.length) return null;
  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="w-full text-xs text-gray-500 hover:text-gray-300 flex items-center justify-center gap-1.5 py-2 border border-dashed border-[#263044] rounded-lg transition-colors"
      >
        <ChevronDown size={13} /> Show {trades.length} trades
      </button>
    );
  }
  const pages = Math.ceil(trades.length / PAGE);
  const slice = trades.slice(page * PAGE, (page + 1) * PAGE);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-300 font-medium">Trades ({trades.length})</span>
        <button onClick={() => setOpen(false)} className="text-[10px] text-gray-600 hover:text-gray-400 flex items-center gap-1">
          <ChevronUp size={11} /> Collapse
        </button>
      </div>
      <div className="overflow-x-auto rounded-lg border border-[#263044]">
        <table className="w-full text-[10px] border-collapse">
          <thead>
            <tr className="bg-[#131c27] text-gray-500 uppercase tracking-wider">
              {['#', 'Entry Time', 'Side', 'Entry', 'Adj', 'SL', 'TP', 'Risk', 'Tgt', 'RR', 'Result', 'Pts', 'R', 'Equity', 'Session', 'Score'].map(h => (
                <th key={h} className="text-left py-1 px-1.5 border-b border-[#263044] whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.map(t => (
              <tr key={t.id} className="border-b border-[#1a2035] hover:bg-[#161b27] transition-colors" title={t.reason}>
                <td className="py-1 px-1.5 font-mono text-gray-600">{t.id}</td>
                <td className="py-1 px-1.5 font-mono text-gray-400 whitespace-nowrap">{t.entryTime?.slice(0, 16).replace('T', ' ')}</td>
                <td className="py-1 px-1.5"><SignalBadge signal={t.signal} /></td>
                <td className="py-1 px-1.5 font-mono text-gray-400">{t.entry?.toFixed(2)}</td>
                <td className="py-1 px-1.5 font-mono text-blue-300">{t.adjustedEntry?.toFixed(2)}</td>
                <td className="py-1 px-1.5 font-mono text-red-400/70">{t.stopLoss?.toFixed(2)}</td>
                <td className="py-1 px-1.5 font-mono text-emerald-400/70">{t.takeProfit?.toFixed(2)}</td>
                <td className="py-1 px-1.5 font-mono text-amber-400">{t.riskPoints?.toFixed(1)}</td>
                <td className="py-1 px-1.5 font-mono text-emerald-400">{t.targetPoints?.toFixed(1)}</td>
                <td className="py-1 px-1.5 font-mono text-blue-300">1:{t.rr?.toFixed(2)}</td>
                <td className="py-1 px-1.5"><ResultBadge result={t.result} /></td>
                <td className={`py-1 px-1.5 font-mono font-bold ${t.points > 0 ? 'text-emerald-400' : t.points < 0 ? 'text-red-400' : 'text-gray-400'}`}>
                  {t.points > 0 ? '+' : ''}{t.points?.toFixed(1)}
                </td>
                <td className={`py-1 px-1.5 font-mono ${t.rMultiple > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {t.rMultiple > 0 ? '+' : ''}{t.rMultiple?.toFixed(2)}R
                </td>
                <td className="py-1 px-1.5 font-mono text-gray-400">{t.equityAfter?.toFixed(0)}</td>
                <td className="py-1 px-1.5 text-gray-400">{t.session}</td>
                <td className="py-1 px-1.5 font-mono text-gray-500">{t.score}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="flex items-center justify-between text-xs text-gray-500 pt-1">
          <button disabled={page === 0} onClick={() => setPage(p => p - 1)}
                  className="px-3 py-1 border border-[#263044] rounded disabled:opacity-30 hover:text-gray-300">Prev</button>
          <span className="font-mono">Page {page + 1} / {pages}</span>
          <button disabled={page === pages - 1} onClick={() => setPage(p => p + 1)}
                  className="px-3 py-1 border border-[#263044] rounded disabled:opacity-30 hover:text-gray-300">Next</button>
        </div>
      )}
    </div>
  );
}

function SkippedList({ skipped = [] }) {
  const [open, setOpen] = useState(false);
  if (!skipped.length) return null;

  // Group reasons
  const byReason = {};
  for (const s of skipped) { byReason[s.reason] = (byReason[s.reason] || 0) + 1; }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="w-full text-xs text-gray-500 hover:text-gray-300 flex items-center justify-between gap-2 px-3 py-2 border border-dashed border-[#263044] rounded-lg transition-colors"
      >
        <span className="flex items-center gap-1.5"><ChevronDown size={13} /> Skipped trades ({skipped.length})</span>
        <span className="flex items-center gap-2 text-[10px] text-gray-600">
          {Object.entries(byReason).slice(0, 3).map(([k, v]) => (
            <span key={k} className="font-mono">{k}: {v}</span>
          ))}
        </span>
      </button>
    );
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-300 font-medium">Skipped Trades ({skipped.length})</span>
        <button onClick={() => setOpen(false)} className="text-[10px] text-gray-600 hover:text-gray-400 flex items-center gap-1">
          <ChevronUp size={11} /> Collapse
        </button>
      </div>
      <div className="overflow-x-auto rounded-lg border border-[#263044] max-h-64 overflow-y-auto">
        <table className="w-full text-[10px]">
          <thead className="sticky top-0 bg-[#131c27]">
            <tr className="text-gray-500 uppercase tracking-wider">
              <th className="text-left py-1 px-1.5 border-b border-[#263044]">Time</th>
              <th className="text-left py-1 px-1.5 border-b border-[#263044]">Side</th>
              <th className="text-left py-1 px-1.5 border-b border-[#263044]">Score</th>
              <th className="text-left py-1 px-1.5 border-b border-[#263044]">Reason</th>
              <th className="text-left py-1 px-1.5 border-b border-[#263044]">Detail</th>
            </tr>
          </thead>
          <tbody>
            {skipped.slice(0, 200).map((s, i) => (
              <tr key={i} className="border-b border-[#1a2035]">
                <td className="py-0.5 px-1.5 font-mono text-gray-500 whitespace-nowrap">{s.time?.slice(0, 16).replace('T', ' ')}</td>
                <td className="py-0.5 px-1.5"><SignalBadge signal={s.signal} /></td>
                <td className="py-0.5 px-1.5 font-mono text-gray-400">{s.score}</td>
                <td className="py-0.5 px-1.5 font-mono text-amber-400/80">{s.reason}</td>
                <td className="py-0.5 px-1.5 text-gray-500">{s.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {skipped.length > 200 && (
          <p className="text-[10px] text-gray-600 italic px-2 py-1">+ {skipped.length - 200} more skipped</p>
        )}
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function XauusdBacktestPanel() {
  const [params, setParams] = useState(DEFAULTS);
  const [startDate, setStartDate] = useState('');
  const [endDate,   setEndDate]   = useState('');
  const [running,   setRunning]   = useState(false);
  const [result,    setResult]    = useState(null);
  const [error,     setError]     = useState(null);
  const [dataStatus, setDataStatus] = useState(null);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await getBacktestDataStatus(params.timeframe);
      setDataStatus(s);
    } catch { /* ignore */ }
  }, [params.timeframe]);

  useEffect(() => { refreshStatus(); }, [refreshStatus]);

  const set = (key, value) => setParams(p => ({ ...p, [key]: value }));

  async function handleRun() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const data = await runXauusdBacktest({
        ...params,
        start_date: startDate || undefined,
        end_date:   endDate   || undefined,
      });
      setResult(data);
    } catch (e) {
      setError(e.message ?? 'Backtest failed');
    } finally {
      setRunning(false);
    }
  }

  const s = result?.summary ?? {};

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl overflow-hidden">

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#263044] bg-[#131c27]">
        <div className="flex items-center gap-2.5">
          <FlaskConical size={15} className="text-blue-400" />
          <div>
            <h2 className="text-sm font-semibold text-slate-200 tracking-wide">Strict XAU/USD Backtest</h2>
            <p className="text-[10px] text-slate-500">
              Walk-forward · No look-ahead bias · Spread &amp; slippage applied · Diagnostic only
            </p>
          </div>
        </div>
        <DataStatusPill status={dataStatus} />
      </div>

      <div className="p-5 space-y-5">

        {/* Settings form */}
        <div className="bg-[#0a0f17] border border-[#1e2535] rounded-lg p-3 space-y-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold flex items-center gap-2">
            <Shield size={11} className="text-blue-400" /> Backtest Settings
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
            <Field label="Start date">
              <Input type="date" value={startDate} onChange={setStartDate} />
            </Field>
            <Field label="End date">
              <Input type="date" value={endDate} onChange={setEndDate} />
            </Field>
            <Field label="Timeframe">
              <Select value={params.timeframe} onChange={v => set('timeframe', v)}
                      options={TIMEFRAMES} />
            </Field>
            <Field label="Lookback" hint="200–20000">
              <Input type="number" value={params.lookback} onChange={v => set('lookback', v)}
                     min={200} max={20000} step={100} />
            </Field>
            <Field label="Initial $">
              <Input type="number" value={params.initial_balance} onChange={v => set('initial_balance', v)}
                     min={100} step={100} />
            </Field>
            <Field label="Risk %" hint="per trade">
              <Input type="number" value={params.risk_percent} onChange={v => set('risk_percent', v)}
                     min={0.05} max={5} step={0.05} />
            </Field>

            <Field label="Spread pts">
              <Input type="number" value={params.spread_points} onChange={v => set('spread_points', v)}
                     min={0} step={0.1} />
            </Field>
            <Field label="Slippage pts">
              <Input type="number" value={params.slippage_points} onChange={v => set('slippage_points', v)}
                     min={0} step={0.1} />
            </Field>
            <Field label="Min score" hint="quality gate">
              <Input type="number" value={params.min_score} onChange={v => set('min_score', v)}
                     min={50} max={100} step={1} />
            </Field>
            <Field label="Min RR">
              <Input type="number" value={params.min_rr} onChange={v => set('min_rr', v)}
                     min={1} max={10} step={0.1} />
            </Field>
            <Field label="News filter">
              <Select value={params.include_news_filter ? 'true' : 'false'}
                      onChange={v => set('include_news_filter', v === 'true')}
                      options={[{ value: 'true', label: 'Enabled' }, { value: 'false', label: 'Disabled' }]} />
            </Field>
            <Field label="Session filter">
              <Select value={params.session_filter ?? ''}
                      onChange={v => set('session_filter', v || undefined)}
                      options={[
                        { value: '',         label: 'All sessions' },
                        { value: 'London',   label: 'London' },
                        { value: 'New York', label: 'New York' },
                        { value: 'Overlap',  label: 'Overlap' },
                        { value: 'Asian',    label: 'Asian' },
                      ]} />
            </Field>
          </div>

          {/* Run + CSV import */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <CsvImportBox onImported={refreshStatus} timeframe={params.timeframe} />
            <div className="lg:col-span-2 flex items-end">
              <button
                onClick={handleRun}
                disabled={running}
                className={`w-full py-3 rounded-lg font-semibold text-sm flex items-center justify-center gap-2 transition-all ${
                  running
                    ? 'bg-blue-900/40 text-blue-300/60 cursor-not-allowed'
                    : 'bg-blue-600 hover:bg-blue-500 text-white shadow-lg shadow-blue-900/20'
                }`}
              >
                <Play size={14} className={running ? 'animate-pulse' : ''} />
                {running ? `Running strict backtest…` : 'Run Backtest'}
              </button>
            </div>
          </div>
        </div>

        {/* Error */}
        {error && (
          <div className="flex items-start gap-2 bg-red-500/10 border border-red-500/25 rounded-lg px-3 py-2.5 text-xs text-red-400">
            <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" /> <span>{error}</span>
          </div>
        )}

        {/* Empty / loading */}
        {!result && !running && !error && (
          <div className="text-center py-10 text-gray-600 text-xs border border-dashed border-[#263044] rounded-lg">
            Adjust settings above, then click <span className="text-gray-400">Run Backtest</span> to simulate the signal engine on historical XAU/USD candles.
          </div>
        )}

        {running && (
          <div className="text-center py-10 text-gray-500 text-xs">
            <div className="inline-flex items-center gap-2">
              <span className="w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              Running {params.lookback}-bar {params.timeframe} backtest…
            </div>
            <p className="mt-2 text-gray-600">Large lookbacks may take several seconds.</p>
          </div>
        )}

        {/* Results */}
        {result && !running && (
          <>
            {/* Warnings + reliability */}
            <ReliabilityCard reliability={result.reliability} />
            <WarningsBanner warnings={result.warnings} />

            {/* Summary grid */}
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
              <Stat label="Valid trades" value={s.validTrades} />
              <Stat label="Win rate"     value={`${s.winRate}%`}
                    color={s.winRate >= 50 ? 'text-emerald-400' : s.winRate >= 40 ? 'text-amber-400' : 'text-red-400'} />
              <Stat label="Expectancy R" value={`${s.expectancyR > 0 ? '+' : ''}${s.expectancyR}R`}
                    color={s.expectancyR > 0 ? 'text-emerald-400' : 'text-red-400'} />
              <Stat label="Profit factor" value={s.profitFactor != null ? s.profitFactor.toFixed(2) : 'N/A'}
                    color={s.profitFactor && s.profitFactor >= 1.5 ? 'text-emerald-400' : s.profitFactor && s.profitFactor >= 1 ? 'text-amber-400' : 'text-red-400'} />
              <Stat label="Max DD %"     value={`${s.maxDrawdownPercent}%`} color="text-red-400" />
              <Stat label="Final $"      value={`$${s.finalBalance?.toFixed(0)}`}
                    color={s.netReturnPercent >= 0 ? 'text-emerald-400' : 'text-red-400'} />
              <Stat label="Net %"        value={`${s.netReturnPercent > 0 ? '+' : ''}${s.netReturnPercent}%`}
                    color={s.netReturnPercent > 0 ? 'text-emerald-400' : 'text-red-400'} />
              <Stat label="Avg RR"       value={`1:${s.averageRR}`} />
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
              <Stat label="Scanned"      value={s.totalSignalsScanned} small />
              <Stat label="Skipped"      value={s.skipped} small />
              <Stat label="Wins"         value={s.wins} color="text-emerald-400" small />
              <Stat label="Losses"       value={s.losses} color="text-red-400" small />
              <Stat label="Expired"      value={s.expired} color="text-purple-400" small />
              <Stat label="Avg Win"      value={`+${s.averageWinPoints}p`} color="text-emerald-400" small />
              <Stat label="Avg Loss"     value={`-${s.averageLossPoints}p`} color="text-red-400" small />
              <Stat label="Avg Held"     value={`${s.averageBarsHeld}b`} small />
            </div>

            {/* Equity curve + drawdown */}
            <EquityCurveChart curve={result.equityCurve} initialBalance={s.initialBalance} />

            {/* Breakdowns */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <BreakdownTable title="Session Performance" rows={result.breakdowns?.session ?? []}
                cols={[
                  { key: 'session',     label: 'Session' },
                  { key: 'trades',      label: '#' },
                  { key: 'winRate',     label: 'Win %', fmt: r => `${r.winRate}%`,
                    cls: r => r.winRate >= 50 ? 'text-emerald-400' : 'text-amber-400' },
                  { key: 'expectancyR', label: 'Exp R',  fmt: r => `${r.expectancyR > 0 ? '+' : ''}${r.expectancyR}R`,
                    cls: r => r.expectancyR > 0 ? 'text-emerald-400' : 'text-red-400' },
                  { key: 'totalPoints', label: 'Pts',    fmt: r => `${r.totalPoints > 0 ? '+' : ''}${r.totalPoints?.toFixed(1)}`,
                    cls: r => r.totalPoints > 0 ? 'text-emerald-400' : 'text-red-400' },
                ]} />
              <BreakdownTable title="Direction Performance" rows={result.breakdowns?.direction ?? []}
                cols={[
                  { key: 'direction',   label: 'Side',
                    cls: r => r.direction === 'BUY' ? 'text-emerald-400' : 'text-red-400' },
                  { key: 'trades',      label: '#' },
                  { key: 'winRate',     label: 'Win %', fmt: r => `${r.winRate}%` },
                  { key: 'expectancyR', label: 'Exp R',  fmt: r => `${r.expectancyR > 0 ? '+' : ''}${r.expectancyR}R`,
                    cls: r => r.expectancyR > 0 ? 'text-emerald-400' : 'text-red-400' },
                  { key: 'totalPoints', label: 'Pts',    fmt: r => `${r.totalPoints > 0 ? '+' : ''}${r.totalPoints?.toFixed(1)}` },
                ]} />
              <BreakdownTable title="Score Band Performance" rows={result.breakdowns?.scoreBand ?? []}
                cols={[
                  { key: 'band',        label: 'Band' },
                  { key: 'trades',      label: '#' },
                  { key: 'winRate',     label: 'Win %', fmt: r => `${r.winRate}%`,
                    cls: r => r.winRate >= 50 ? 'text-emerald-400' : 'text-amber-400' },
                  { key: 'expectancyR', label: 'Exp R',  fmt: r => `${r.expectancyR > 0 ? '+' : ''}${r.expectancyR}R`,
                    cls: r => r.expectancyR > 0 ? 'text-emerald-400' : 'text-red-400' },
                ]} />
              <BreakdownTable title="Result Distribution" rows={result.breakdowns?.result ?? []}
                cols={[
                  { key: 'result',  label: 'Result' },
                  { key: 'count',   label: 'Count' },
                  { key: 'pct',     label: '%', fmt: r => `${r.pct}%` },
                ]} />
            </div>

            {/* Trades + skipped */}
            <TradeList trades={result.trades} />
            <SkippedList skipped={result.skipped} />

            {/* Saved run id */}
            {result.savedRunId && (
              <p className="text-[10px] text-gray-600 italic">
                <History size={9} className="inline mr-1" />
                Saved as backtest run #{result.savedRunId} — available at GET /api/v1/backtest/runs/{result.savedRunId}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
