/**
 * PaperTradePanel
 *
 * The core manual workflow component:
 *   1. Run Analysis  → calls POST /signal/analyze
 *   2. Review result → shows signal, quality score, entry/SL/TP, ICT components
 *   3. Confirm       → saves to DB via POST /signal/confirm
 *   4. Reminder      → tells you to place the trade manually in your broker
 *
 * After you've placed the trade manually, go to Signal History below
 * and click "Log Result" when it closes.
 */
import { useState } from 'react';
import {
  Play, CheckCircle, AlertTriangle, XCircle, Clock,
  TrendingUp, TrendingDown, Minus, ChevronDown, ChevronUp,
  BookOpen, Shield,
} from 'lucide-react';
import { confirmSignal } from '../services/api';

// ── helpers ───────────────────────────────────────────────────────────────────

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

async function runAnalysis(macroEvents = [], pairCode = 'eurusd') {
  const res = await fetch(`${API_BASE}/api/v1/signal/analyze?pair=${pairCode}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(macroEvents),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

async function saveSignal(signal) {
  const res = await fetch(`${API_BASE}/api/v1/signal/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pair:             pairLabel,
      timeframe:        'H4',
      signal:           signal.signal,
      quality_score:    signal.qualityScore,
      entry:            signal.entry,
      stop_loss:        signal.stopLoss,
      take_profit:      signal.takeProfit,
      risk_pips:        signal.riskPips,
      target_pips:      signal.targetPips,
      rr:               signal.rr,
      invalidation:     signal.invalidation,
      liquidity_status: signal.model?.liquidity        ?? '',
      fvg_status:       signal.model?.fvg              ?? '',
      structure_status: signal.model?.structure        ?? '',
      macro_status:     signal.model?.higherTimeframeBias ?? '',
      news_status:      signal.newsStatus              ?? 'CLEAR',
      session:          signal.model?.session          ?? '',
      reason:           signal.reason                  ?? '',
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── sub-components ────────────────────────────────────────────────────────────

function SignalBadge({ signal }) {
  const cfg = {
    BUY:  { cls: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40', Icon: TrendingUp,  label: 'BUY' },
    SELL: { cls: 'bg-red-500/20 text-red-400 border-red-500/40',             Icon: TrendingDown, label: 'SELL' },
    WAIT: { cls: 'bg-amber-500/20 text-amber-400 border-amber-500/40',       Icon: Minus,        label: 'WAIT' },
  }[signal] ?? { cls: 'bg-gray-500/20 text-gray-400 border-gray-500/40', Icon: Minus, label: signal };

  const { cls, Icon, label } = cfg;
  return (
    <span className={`inline-flex items-center gap-2 px-4 py-2 rounded-xl border font-black text-2xl tracking-widest ${cls}`}>
      <Icon size={20} />
      {label}
    </span>
  );
}

function ScoreRing({ score }) {
  const color = score >= 80 ? 'text-emerald-400' : score >= 60 ? 'text-amber-400' : 'text-red-400';
  const label = score >= 80 ? 'Strong' : score >= 60 ? 'Moderate' : 'Weak';
  return (
    <div className="text-center">
      <div className={`text-4xl font-black font-mono ${color}`}>{score}</div>
      <div className="text-[10px] text-gray-500 uppercase tracking-wider">/100 · {label}</div>
    </div>
  );
}

function ComponentRow({ label, text, positive }) {
  const Icon = positive ? CheckCircle : XCircle;
  const cls  = positive ? 'text-emerald-400' : 'text-gray-600';
  return (
    <div className="flex items-start gap-2 py-1.5 border-b border-[#1e2535] last:border-0">
      <Icon size={13} className={`${cls} flex-shrink-0 mt-0.5`} />
      <div>
        <span className="text-[10px] text-gray-500 uppercase tracking-wider">{label}</span>
        <p className="text-xs text-gray-300 mt-0.5">{text || '—'}</p>
      </div>
    </div>
  );
}

function PriceBox({ label, value, cls = 'text-white', decimals = 5 }) {
  return (
    <div className="bg-[#1e2535] rounded-lg p-3 text-center">
      <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">{label}</div>
      <div className={`font-mono text-sm font-bold ${cls}`}>
        {value != null ? value.toFixed(decimals) : '—'}
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function PaperTradePanel({ onConfirmed, selectedPair }) {
  const pairCode     = selectedPair?.code     ?? 'eurusd';
  const pairLabel    = selectedPair?.label    ?? 'EUR/USD';
  const pairDecimals = selectedPair?.decimals ?? 5;

  const [running,   setRunning]   = useState(false);
  const [result,    setResult]    = useState(null);
  const [error,     setError]     = useState(null);
  const [saving,    setSaving]    = useState(false);
  const [saved,     setSaved]     = useState(null);   // saved DB record
  const [showModel, setShowModel] = useState(false);

  async function handleRunAnalysis() {
    setRunning(true);
    setError(null);
    setResult(null);
    setSaved(null);
    try {
      const data = await runAnalysis([], pairCode);
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  }

  async function handleConfirm() {
    if (!result) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveSignal(result);
      setSaved(saved.data ?? saved);
      onConfirmed?.();      // tell Dashboard to refresh history
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  const isTradeable = result && (result.signal === 'BUY' || result.signal === 'SELL');
  const model = result?.model ?? {};

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-5">

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <BookOpen size={15} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
            Paper Trading — {pairLabel}
          </h2>
        </div>
        <span className="text-[10px] text-slate-500 bg-slate-800 px-2 py-0.5 rounded-full border border-slate-700">
          Demo Mode · No Real Orders
        </span>
      </div>

      {/* ── Step 1: Run Analysis ─────────────────────────────────────────── */}
      <div className="space-y-2">
        <p className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">
          Step 1 — Run the ICT Signal Engine
        </p>
        <button
          onClick={handleRunAnalysis}
          disabled={running}
          className="w-full py-3 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold text-sm flex items-center justify-center gap-2 transition-colors"
        >
          <Play size={14} className={running ? 'animate-pulse' : ''} />
          {running ? 'Analysing candles…' : 'Run Live Analysis'}
        </button>
        {error && (
          <div className="flex items-center gap-2 text-xs text-red-400 bg-red-900/20 border border-red-800/40 rounded-lg px-3 py-2">
            <AlertTriangle size={13} />
            {error}
          </div>
        )}
      </div>

      {/* ── Step 2: Review Result ─────────────────────────────────────────── */}
      {result && (
        <div className="space-y-4 border-t border-[#263044] pt-4">
          <p className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">
            Step 2 — Review the Setup
          </p>

          {/* Signal + score */}
          <div className="flex items-center justify-between bg-[#131c27] rounded-xl p-4">
            <SignalBadge signal={result.signal} />
            <ScoreRing score={result.qualityScore} />
          </div>

          {/* Reason / news status */}
          <div className={`flex items-start gap-2 rounded-lg px-3 py-2 text-xs ${
            result.newsStatus === 'BLOCKED'
              ? 'bg-red-900/20 border border-red-800/40 text-red-300'
              : isTradeable
                ? 'bg-emerald-900/15 border border-emerald-800/30 text-emerald-300'
                : 'bg-amber-900/15 border border-amber-800/30 text-amber-300'
          }`}>
            {result.newsStatus === 'BLOCKED'
              ? <XCircle size={13} className="flex-shrink-0 mt-0.5" />
              : isTradeable
                ? <CheckCircle size={13} className="flex-shrink-0 mt-0.5" />
                : <Clock size={13} className="flex-shrink-0 mt-0.5" />
            }
            <span>{isTradeable ? 'All gates passed — setup is valid' : (result.reason || 'Waiting for setup conditions')}</span>
          </div>

          {/* Trade geometry — only shown when tradeable */}
          {isTradeable && (
            <div className="grid grid-cols-3 gap-2">
              <PriceBox label="Entry"      value={result.entry}      cls="text-blue-400"    decimals={pairDecimals} />
              <PriceBox label="Stop Loss"  value={result.stopLoss}   cls="text-red-400"     decimals={pairDecimals} />
              <PriceBox label="Take Profit" value={result.takeProfit} cls="text-emerald-400" decimals={pairDecimals} />
            </div>
          )}

          {isTradeable && (
            <div className="grid grid-cols-3 gap-2 text-center text-xs">
              <div className="bg-[#1e2535] rounded-lg p-2">
                <div className="text-[10px] text-gray-500">Risk Pips</div>
                <div className="font-mono font-bold text-red-400">{result.riskPips}</div>
              </div>
              <div className="bg-[#1e2535] rounded-lg p-2">
                <div className="text-[10px] text-gray-500">Target Pips</div>
                <div className="font-mono font-bold text-emerald-400">{result.targetPips}</div>
              </div>
              <div className="bg-[#1e2535] rounded-lg p-2">
                <div className="text-[10px] text-gray-500">R : R</div>
                <div className="font-mono font-bold text-blue-400">1:{result.rr}</div>
              </div>
            </div>
          )}

          {/* ICT component breakdown (collapsible) */}
          <button
            onClick={() => setShowModel(m => !m)}
            className="w-full flex items-center justify-between text-[10px] text-slate-500 hover:text-slate-400 transition-colors py-1"
          >
            <span className="uppercase tracking-widest font-semibold">ICT Components</span>
            {showModel ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>

          {showModel && (
            <div className="bg-[#131c27] rounded-xl p-3 space-y-0.5">
              <ComponentRow label="HTF Bias"   text={model.higherTimeframeBias} positive={!!model.higherTimeframeBias && !model.higherTimeframeBias.includes('Neutral')} />
              <ComponentRow label="Liquidity"  text={model.liquidity}  positive={model.liquidity?.includes('swept')} />
              <ComponentRow label="Structure"  text={model.structure}  positive={model.structure?.includes('BOS') || model.structure?.includes('CHoCH')} />
              <ComponentRow label="FVG"        text={model.fvg}        positive={model.fvg?.includes('pip') && !model.fvg?.includes('No')} />
              <ComponentRow label="Session"    text={model.session}    positive={model.session?.includes('kill zone') || model.session?.includes('Overlap')} />
            </div>
          )}
        </div>
      )}

      {/* ── Step 3: Confirm & Save ────────────────────────────────────────── */}
      {result && !saved && (
        <div className="border-t border-[#263044] pt-4 space-y-3">
          <p className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">
            Step 3 — Log to Your Journal
          </p>
          <p className="text-xs text-slate-400">
            {isTradeable
              ? 'Save this setup to your signal database, then place the trade manually in your broker.'
              : 'Save this WAIT signal to track what the engine saw at this time.'}
          </p>
          <button
            onClick={handleConfirm}
            disabled={saving}
            className={`w-full py-2.5 rounded-xl font-semibold text-sm flex items-center justify-center gap-2 transition-colors disabled:opacity-50 ${
              isTradeable
                ? 'bg-emerald-700 hover:bg-emerald-600 text-white'
                : 'bg-slate-700 hover:bg-slate-600 text-slate-200'
            }`}
          >
            <CheckCircle size={14} />
            {saving ? 'Saving…' : 'Confirm & Save to Journal'}
          </button>
        </div>
      )}

      {/* ── Step 4: Placement reminder ────────────────────────────────────── */}
      {saved && (
        <div className="border-t border-[#263044] pt-4 space-y-3">
          <div className="flex items-center gap-2 text-emerald-400 text-sm font-semibold">
            <CheckCircle size={16} />
            Saved to journal (ID #{saved.id ?? '—'})
          </div>

          {isTradeable && (
            <div className="rounded-xl border border-blue-800/40 bg-blue-900/10 p-4 space-y-2">
              <div className="flex items-center gap-2 text-xs font-semibold text-blue-300">
                <Shield size={13} />
                Step 4 — Place Manually in Your Broker
              </div>
              <ul className="text-xs text-blue-200/80 space-y-1 list-disc list-inside">
                <li>Direction: <strong>{result.signal}</strong></li>
                <li>Entry near: <strong className="font-mono">{result.entry?.toFixed(pairDecimals)}</strong></li>
                <li>Stop Loss: <strong className="font-mono">{result.stopLoss?.toFixed(pairDecimals)}</strong></li>
                <li>Take Profit: <strong className="font-mono">{result.takeProfit?.toFixed(pairDecimals)}</strong></li>
                <li>Invalidation: <strong className="font-mono">{result.invalidation?.toFixed(pairDecimals)}</strong></li>
              </ul>
              <p className="text-[11px] text-blue-300/60 mt-2">
                When the trade closes — come back to Signal History below and click <strong>Log Result</strong> on this row.
              </p>
            </div>
          )}

          <button
            onClick={() => { setResult(null); setSaved(null); setError(null); }}
            className="w-full py-2 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 text-xs transition-colors"
          >
            Run Another Analysis
          </button>
        </div>
      )}
    </section>
  );
}
