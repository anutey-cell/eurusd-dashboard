/**
 * PaperTradeJournal
 *
 * Shows every confirmed signal saved to the local SQLite database.
 * Rows without a result have a "Log Result" button that expands an inline form.
 *
 * Data flow:
 *   GET /api/v1/signal/db/history  →  list of SignalRead records
 *   PUT /api/v1/signal/{id}/result →  record WIN / LOSS / BREAKEVEN
 */
import { useState, useEffect, useCallback } from 'react';
import {
  BookOpen, RefreshCw, TrendingUp, TrendingDown, Minus,
  CheckCircle, XCircle, Clock, ChevronDown, ChevronUp,
  Save, X,
} from 'lucide-react';
import { getDbSignalHistory, logSignalResult } from '../services/api';

// ── helpers ───────────────────────────────────────────────────────────────────

const RESULT_STYLE = {
  WIN:       'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
  LOSS:      'bg-red-500/15 text-red-400 border border-red-500/30',
  BREAKEVEN: 'bg-gray-500/15 text-gray-400 border border-gray-500/30',
};

function SignalPill({ signal }) {
  if (signal === 'BUY')  return <span className="flex items-center gap-1 text-emerald-400 font-semibold"><TrendingUp size={11} />BUY</span>;
  if (signal === 'SELL') return <span className="flex items-center gap-1 text-red-400 font-semibold"><TrendingDown size={11} />SELL</span>;
  return <span className="flex items-center gap-1 text-amber-400 font-semibold"><Minus size={11} />WAIT</span>;
}

function ResultBadge({ result }) {
  if (!result) return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30">
      <Clock size={9} />PENDING
    </span>
  );
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold ${RESULT_STYLE[result] ?? 'text-gray-400'}`}>
      {result}
    </span>
  );
}

// ── Log-result inline form ────────────────────────────────────────────────────

function LogResultForm({ record, onSaved, onCancel }) {
  const [form, setForm] = useState({
    result:    'WIN',
    exitPrice: record.entry?.toFixed(5) ?? '',
    pips:      '',
    pnl:       '',
    notes:     '',
  });
  const [saving, setSaving]   = useState(false);
  const [err,    setErr]      = useState(null);

  // Auto-compute pips from exit price when user types
  function handleExitChange(val) {
    setForm(f => {
      const exit  = parseFloat(val);
      const entry = record.entry;
      let pips    = f.pips;
      if (!isNaN(exit) && entry != null) {
        const rawPips = (exit - entry) * (record.pair?.includes('JPY') ? 100 : 10000);
        pips = String(Math.round(record.signal === 'SELL' ? -rawPips : rawPips));
      }
      return { ...f, exitPrice: val, pips };
    });
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const pips = parseInt(form.pips, 10);
    if (isNaN(pips)) { setErr('Pips must be a number'); return; }
    const exitPrice = parseFloat(form.exitPrice);
    if (isNaN(exitPrice)) { setErr('Exit price must be a number'); return; }

    setSaving(true);
    setErr(null);
    try {
      await logSignalResult(record.id, {
        result:    form.result,
        exitPrice,
        pips,
        pnl:   form.pnl ? parseFloat(form.pnl) : null,
        notes: form.notes || null,
      });
      onSaved();
    } catch (ex) {
      setErr(ex.message);
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="bg-[#131c27] border border-blue-800/30 rounded-xl p-4 space-y-3">
      <p className="text-[10px] text-blue-300 uppercase tracking-widest font-semibold">
        Log Result — Signal #{record.id}
      </p>

      {/* Result selector */}
      <div className="flex gap-2">
        {['WIN', 'LOSS', 'BREAKEVEN'].map(r => (
          <button
            key={r}
            type="button"
            onClick={() => setForm(f => ({ ...f, result: r }))}
            className={`flex-1 py-1.5 rounded-lg text-xs font-bold border transition-colors ${
              form.result === r
                ? r === 'WIN'       ? 'bg-emerald-700 border-emerald-600 text-white'
                : r === 'LOSS'      ? 'bg-red-700 border-red-600 text-white'
                                    : 'bg-slate-600 border-slate-500 text-white'
                : 'bg-transparent border-[#263044] text-gray-500 hover:text-gray-300'
            }`}
          >
            {r === 'WIN' && <CheckCircle size={11} className="inline mr-1" />}
            {r === 'LOSS' && <XCircle size={11} className="inline mr-1" />}
            {r}
          </button>
        ))}
      </div>

      {/* Price + pip row */}
      <div className="grid grid-cols-3 gap-2">
        <div>
          <label className="block text-[10px] text-gray-500 mb-1">Exit Price</label>
          <input
            type="number"
            step="0.00001"
            value={form.exitPrice}
            onChange={e => handleExitChange(e.target.value)}
            className="w-full bg-[#0d1117] border border-[#263044] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:border-blue-600 outline-none"
            required
          />
        </div>
        <div>
          <label className="block text-[10px] text-gray-500 mb-1">Pips (± auto)</label>
          <input
            type="number"
            value={form.pips}
            onChange={e => setForm(f => ({ ...f, pips: e.target.value }))}
            className="w-full bg-[#0d1117] border border-[#263044] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:border-blue-600 outline-none"
            required
          />
        </div>
        <div>
          <label className="block text-[10px] text-gray-500 mb-1">P&amp;L ($, opt.)</label>
          <input
            type="number"
            step="0.01"
            value={form.pnl}
            onChange={e => setForm(f => ({ ...f, pnl: e.target.value }))}
            placeholder="—"
            className="w-full bg-[#0d1117] border border-[#263044] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:border-blue-600 outline-none"
          />
        </div>
      </div>

      {/* Notes */}
      <div>
        <label className="block text-[10px] text-gray-500 mb-1">Notes (optional)</label>
        <input
          type="text"
          value={form.notes}
          onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
          placeholder="What worked / what didn't…"
          className="w-full bg-[#0d1117] border border-[#263044] rounded-lg px-2 py-1.5 text-xs text-white focus:border-blue-600 outline-none"
        />
      </div>

      {err && (
        <p className="text-xs text-red-400 bg-red-900/20 border border-red-800/30 rounded px-2 py-1">{err}</p>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={saving}
          className="flex-1 py-2 rounded-lg bg-blue-700 hover:bg-blue-600 disabled:opacity-50 text-white text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
        >
          <Save size={12} />
          {saving ? 'Saving…' : 'Save Result'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-2 rounded-lg border border-[#263044] text-gray-500 hover:text-gray-300 text-xs transition-colors"
        >
          <X size={12} />
        </button>
      </div>
    </form>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function PaperTradeJournal({ refreshKey = 0 }) {
  const [records,    setRecords]    = useState([]);
  const [total,      setTotal]      = useState(0);
  const [loading,    setLoading]    = useState(true);
  const [error,      setError]      = useState(null);
  const [openForm,   setOpenForm]   = useState(null);   // signal ID with open form
  const [filterSig,  setFilterSig]  = useState('ALL');  // ALL BUY SELL WAIT
  const [showAll,    setShowAll]    = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const sig    = filterSig !== 'ALL' ? filterSig : undefined;
      const data   = await getDbSignalHistory({ page: 1, pageSize: 50, signal: sig });
      setRecords(data.signals ?? []);
      setTotal(data.total ?? 0);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [filterSig]);

  // reload when parent calls onConfirmed (refreshKey bumped) or filter changes
  useEffect(() => { load(); }, [load, refreshKey]);

  const pending  = records.filter(r => !r.result);
  const settled  = records.filter(r =>  r.result);
  const shown    = showAll ? records : records.slice(0, 10);

  return (
    <section className="bg-[#0d1117] border border-[#263044] rounded-xl p-5 space-y-4">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <BookOpen size={15} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-slate-200 tracking-wide">
            Paper Trading Journal
          </h2>
          <span className="text-[10px] text-gray-600">({total} saved)</span>
          {pending.length > 0 && (
            <span className="text-[10px] bg-amber-500/10 text-amber-400 border border-amber-500/30 px-1.5 py-0.5 rounded">
              {pending.length} pending
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* Signal filter */}
          <div className="flex bg-[#131c27] rounded-lg border border-[#263044] p-0.5 gap-0.5">
            {['ALL', 'BUY', 'SELL', 'WAIT'].map(f => (
              <button
                key={f}
                onClick={() => { setFilterSig(f); setOpenForm(null); }}
                className={`px-2 py-0.5 text-[10px] font-medium rounded-md transition-colors ${
                  filterSig === f
                    ? f === 'BUY'  ? 'bg-emerald-700 text-white'
                    : f === 'SELL' ? 'bg-red-700 text-white'
                    : f === 'WAIT' ? 'bg-amber-700 text-white'
                    : 'bg-[#263044] text-white'
                    : 'text-gray-500 hover:text-gray-300'
                }`}
              >{f}</button>
            ))}
          </div>
          <button
            onClick={load}
            title="Refresh"
            className="text-gray-600 hover:text-gray-400 transition-colors"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Stats bar */}
      {records.length > 0 && (
        <div className="flex gap-4 text-[11px]">
          <span className="text-emerald-400 font-mono font-semibold">
            {settled.filter(r => r.result === 'WIN').length}W
          </span>
          <span className="text-red-400 font-mono font-semibold">
            {settled.filter(r => r.result === 'LOSS').length}L
          </span>
          <span className="text-gray-500 font-mono">
            {settled.filter(r => r.result === 'BREAKEVEN').length}BE
          </span>
          <span className="text-amber-400 font-mono">{pending.length} open</span>
          {settled.length > 0 && (
            <span className={`font-mono font-semibold ml-auto ${
              settled.reduce((a, r) => a + (r.pips ?? 0), 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
            }`}>
              {settled.reduce((a, r) => a + (r.pips ?? 0), 0) >= 0 ? '+' : ''}
              {settled.reduce((a, r) => a + (r.pips ?? 0), 0)} pips
            </span>
          )}
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="text-xs text-red-400 bg-red-900/15 border border-red-800/30 rounded-lg px-3 py-2">
          {error} —{' '}
          <button onClick={load} className="underline">retry</button>
        </div>
      )}

      {/* Empty state */}
      {!loading && records.length === 0 && !error && (
        <div className="text-center py-8 text-gray-600 text-sm">
          No confirmed signals yet. Run an analysis above and click{' '}
          <strong className="text-gray-500">Confirm &amp; Save to Journal</strong>.
        </div>
      )}

      {/* Records list */}
      {!loading && records.length > 0 && (
        <div className="space-y-2">
          {shown.map(rec => (
            <div key={rec.id}>
              {/* Row card */}
              <div className={`bg-[#131c27] rounded-xl p-3 flex flex-wrap items-center gap-3 ${
                openForm === rec.id ? 'border border-blue-800/40' : 'border border-transparent hover:border-[#263044]'
              } transition-colors`}>

                {/* ID + date */}
                <div className="flex-shrink-0 text-center">
                  <div className="text-[10px] text-gray-600 font-mono">#{rec.id}</div>
                  <div className="text-[10px] text-gray-500 font-mono">
                    {rec.createdAt
                      ? new Date(rec.createdAt).toLocaleDateString()
                      : '—'}
                  </div>
                </div>

                {/* Direction */}
                <div className="w-14 flex-shrink-0 text-xs">
                  <SignalPill signal={rec.signal} />
                  <div className="text-[10px] text-gray-600 mt-0.5">{rec.pair}</div>
                </div>

                {/* Quality score */}
                <div className="flex-shrink-0 text-center">
                  <div className={`text-sm font-black font-mono ${
                    rec.qualityScore >= 80 ? 'text-emerald-400'
                    : rec.qualityScore >= 60 ? 'text-amber-400'
                    : 'text-red-400'
                  }`}>{rec.qualityScore}</div>
                  <div className="text-[9px] text-gray-600">score</div>
                </div>

                {/* Entry / SL / TP */}
                {rec.entry != null && (
                  <div className="flex gap-2 text-[10px] font-mono flex-shrink-0">
                    <div>
                      <div className="text-gray-600">Entry</div>
                      <div className="text-blue-300">{rec.entry?.toFixed(5)}</div>
                    </div>
                    <div>
                      <div className="text-gray-600">SL</div>
                      <div className="text-red-400">{rec.stopLoss?.toFixed(5) ?? '—'}</div>
                    </div>
                    <div>
                      <div className="text-gray-600">TP</div>
                      <div className="text-emerald-400">{rec.takeProfit?.toFixed(5) ?? '—'}</div>
                    </div>
                  </div>
                )}

                {/* RR */}
                {rec.rr != null && (
                  <div className="flex-shrink-0 text-center">
                    <div className="text-xs font-mono font-bold text-blue-400">1:{rec.rr}</div>
                    <div className="text-[9px] text-gray-600">R:R</div>
                  </div>
                )}

                {/* Session */}
                <div className="flex-1 min-w-0 text-[10px] text-gray-600 truncate hidden sm:block">
                  {rec.session || rec.reason || '—'}
                </div>

                {/* Result badge OR Log Result button */}
                <div className="flex-shrink-0 flex items-center gap-2">
                  {rec.result ? (
                    <div className="text-right">
                      <ResultBadge result={rec.result} />
                      {rec.pips != null && (
                        <div className={`text-[10px] font-mono mt-0.5 ${rec.pips >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {rec.pips >= 0 ? '+' : ''}{rec.pips} pips
                        </div>
                      )}
                    </div>
                  ) : (
                    <button
                      onClick={() => setOpenForm(openForm === rec.id ? null : rec.id)}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-700/20 border border-blue-600/30 text-blue-300 hover:bg-blue-700/40 text-[10px] font-semibold transition-colors"
                    >
                      {openForm === rec.id
                        ? <><ChevronUp size={10} />Cancel</>
                        : <><ChevronDown size={10} />Log Result</>}
                    </button>
                  )}
                </div>
              </div>

              {/* Inline form */}
              {openForm === rec.id && (
                <div className="mt-1 px-1">
                  <LogResultForm
                    record={rec}
                    onSaved={() => { setOpenForm(null); load(); }}
                    onCancel={() => setOpenForm(null)}
                  />
                </div>
              )}
            </div>
          ))}

          {/* Show more / less */}
          {records.length > 10 && (
            <button
              onClick={() => setShowAll(v => !v)}
              className="w-full py-1.5 text-[11px] text-gray-600 hover:text-gray-400 flex items-center justify-center gap-1 transition-colors"
            >
              {showAll
                ? <><ChevronUp size={11} />Show less</>
                : <><ChevronDown size={11} />Show all {records.length} records</>}
            </button>
          )}
        </div>
      )}

      {loading && (
        <div className="flex items-center justify-center py-6 gap-2 text-xs text-gray-600">
          <RefreshCw size={12} className="animate-spin" />
          Loading journal…
        </div>
      )}
    </section>
  );
}
