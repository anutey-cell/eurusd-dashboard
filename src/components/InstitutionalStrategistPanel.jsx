/**
 * InstitutionalStrategistPanel
 *
 * The executive verdict — synthesises every engine into a single
 * institutional-grade decision: LONG / SHORT / STAND ASIDE.
 *
 * Mandate: protect capital first. Default action is STAND ASIDE.
 * Trade fires only when 5 gates align AND setup score >= 80 AND RR >= 2.5.
 *
 * Polls /strategist/decision every 30s (cached server-side 60s). Click
 * Refresh to force-fresh.
 */
import { useState, useCallback } from 'react';
import {
  TrendingUp, TrendingDown, MinusCircle, RefreshCw, AlertTriangle,
  CheckCircle, Lock, Target, Activity, Compass, Shield,
} from 'lucide-react';
import { getStrategistDecision, refreshStrategistDecision } from '../services/api';
import { formatKenyaTime, KENYA_LABEL } from '../utils/time';
import { usePollInterval } from '../hooks/usePollInterval';

const POLL_MS = 30_000;

const DECISION_CFG = {
  // Mandate primary decision values
  BUY:           { cls: 'border-emerald-500/70 bg-emerald-500/15 text-emerald-300',
                   Icon: TrendingUp, label: 'BUY' },
  SELL:          { cls: 'border-red-500/70     bg-red-500/15     text-red-300',
                   Icon: TrendingDown, label: 'SELL' },
  // Legacy aliases (kept so older cached verdicts still render)
  LONG:          { cls: 'border-emerald-500/70 bg-emerald-500/15 text-emerald-300',
                   Icon: TrendingUp, label: 'BUY' },
  SHORT:         { cls: 'border-red-500/70     bg-red-500/15     text-red-300',
                   Icon: TrendingDown, label: 'SELL' },
  'STAND ASIDE': { cls: 'border-amber-500/50   bg-amber-500/10   text-amber-300',
                   Icon: MinusCircle, label: 'STAND ASIDE' },
};

const EXECUTION_STATUS_CLS = {
  DEMO_TRADE_PLACED:        'bg-emerald-600/30 text-emerald-200 border-emerald-500/60',
  SIGNAL_ONLY:              'bg-blue-600/30    text-blue-200    border-blue-500/60',
  STAND_ASIDE:              'bg-amber-600/30   text-amber-200   border-amber-500/60',
  BRIDGE_OFFLINE:           'bg-orange-600/30  text-orange-200  border-orange-500/60',
  SPREAD_TOO_HIGH:          'bg-orange-600/30  text-orange-200  border-orange-500/60',
  NEWS_RISK_BLOCKED:        'bg-orange-600/30  text-orange-200  border-orange-500/60',
  DEMO_TRADE_REJECTED:      'bg-red-600/30     text-red-200     border-red-500/60',
  INVALIDATED_BEFORE_ENTRY: 'bg-red-600/30     text-red-200     border-red-500/60',
};

function PriceLine({ label, value, valueCls = 'text-white' }) {
  return (
    <div className="flex justify-between items-center text-xs py-0.5 border-b border-[#1c2333] last:border-0">
      <span className="text-gray-500">{label}</span>
      <span className={`font-mono ${valueCls}`}>{value ?? '—'}</span>
    </div>
  );
}

function Block({ icon: Icon, title, children, accent = 'border-[#263044]' }) {
  return (
    <div className={`rounded border ${accent} bg-[#161b27] p-3 space-y-1.5`}>
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-gray-400 font-bold">
        {Icon && <Icon size={11} />} {title}
      </div>
      {children}
    </div>
  );
}

export default function InstitutionalStrategistPanel() {
  const [v, setV] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const r = await getStrategistDecision();
      setV(r);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Strategist endpoint unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  const forceRefresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await refreshStrategistDecision();
      setV(r);
      setError(null);
    } catch (e) {
      setError(e?.message ?? 'Refresh failed');
    } finally {
      setLoading(false);
    }
  }, []);

  usePollInterval(load, POLL_MS);

  const dec = v?.decision || 'STAND ASIDE';
  const cfg = DECISION_CFG[dec] || DECISION_CFG['STAND ASIDE'];

  return (
    <div className="bg-[#0d1117] border-2 border-[#263044] rounded-xl p-5 space-y-4">

      {/* Top banner: BIG decision + 5-condition score */}
      <div className={`rounded-lg border-2 p-4 ${cfg.cls}`}>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <cfg.Icon size={40} />
            <div>
              <div className="text-[10px] uppercase tracking-widest opacity-80">
                Institutional Demo Strategist · XAUUSD · 0.01 lot
              </div>
              <div className="text-3xl font-bold leading-tight">{cfg.label}</div>
              <div className="text-xs opacity-90 mt-0.5">{v?.final_verdict || '—'}</div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-[10px] uppercase tracking-widest opacity-70">Conditions</div>
            <div className="text-4xl font-mono font-bold leading-none">
              {v?.conditions_passed ?? 0}<span className="text-base opacity-50">/5</span>
            </div>
            <div className="text-[10px] opacity-80 font-mono mt-0.5">
              est WR {v?.estimated_win_rate_range || '—'}
            </div>
            <div className={`mt-1 inline-block text-[10px] font-bold uppercase tracking-widest px-2 py-0.5 rounded border ${EXECUTION_STATUS_CLS[v?.execution_status] || EXECUTION_STATUS_CLS.STAND_ASIDE}`}>
              {v?.execution_status || 'STAND_ASIDE'}
            </div>
          </div>
        </div>
        <div className="text-xs opacity-90 mt-3 pl-12">
          {v?.market_state} · {v?.market_sentiment} · {v?.timeframe_alignment?.alignment_summary}
        </div>
        <div className="text-[10px] opacity-70 mt-1 pl-12 font-mono">
          {v?.session_classification} · {v?.liquidity_behaviour} · score {v?.setup_score ?? 0}/100 ({v?.quality_band})
        </div>
      </div>

      {/* 5-condition checklist */}
      {v?.conditions?.length > 0 && (
        <div className="bg-[#161b27] border border-[#263044] rounded p-3">
          <div className="text-[10px] uppercase tracking-widest text-gray-400 font-bold mb-2">
            5-Condition Mandate Score
          </div>
          <div className="space-y-1">
            {v.conditions.map((c, i) => (
              <div key={i} className="flex items-start gap-2 text-[11px]">
                {c.passed
                  ? <CheckCircle size={12} className="text-emerald-400 mt-0.5 flex-shrink-0" />
                  : <MinusCircle size={12} className="text-gray-500 mt-0.5 flex-shrink-0" />}
                <div className="flex-1">
                  <div className={c.passed ? 'text-gray-200' : 'text-gray-500'}>{c.name}</div>
                  <div className="text-[10px] text-gray-500 font-mono">{c.detail}</div>
                </div>
              </div>
            ))}
          </div>
          {v.improvement_note && (
            <div className="mt-2 pt-2 border-t border-[#263044] text-[10px] text-amber-300/80 italic">
              {v.improvement_note}
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="bg-red-500/10 border border-red-500/40 rounded p-2 text-xs text-red-300 flex items-center gap-2">
          <AlertTriangle size={11} /> {error}
        </div>
      )}

      {/* STAND ASIDE — show the reason and next-triggers */}
      {dec === 'STAND ASIDE' && v?.stand_aside_reason && (
        <div className="rounded border border-amber-500/40 bg-amber-500/5 p-3">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold text-amber-300 mb-1">
            <AlertTriangle size={11} /> Stand-Aside Rationale
          </div>
          <div className="text-xs text-amber-200 mb-2">{v.stand_aside_reason}</div>
          {v.next_trigger && (
            <div className="space-y-1 text-[10px] text-amber-200/90 leading-snug border-t border-amber-500/20 pt-2">
              <div><span className="text-emerald-300 font-bold">BUY valid IF:</span> {v.next_trigger.long_trigger || '—'}</div>
              <div><span className="text-red-300 font-bold">SELL valid IF:</span> {v.next_trigger.short_trigger || '—'}</div>
              <div className="text-amber-200/70 italic">{v.next_trigger.no_trade_condition}</div>
            </div>
          )}
        </div>
      )}

      {/* Trade plan — only if a real decision */}
      {(dec === 'BUY' || dec === 'SELL' || dec === 'LONG' || dec === 'SHORT') && v?.trade_plan && (
        <Block icon={Target} title="Trade Plan (DEMO · 0.01 lot)" accent="border-emerald-500/40">
          <PriceLine label="Entry"        value={`$${v.trade_plan.entry} ± $${v.trade_plan.entry_tolerance ?? 0}`} valueCls="text-white font-bold" />
          <PriceLine label="Stop Loss"    value={`$${v.trade_plan.stop_loss}`} valueCls="text-red-300" />
          <PriceLine label="TP1"          value={`$${v.trade_plan.tp1}`} valueCls="text-emerald-300" />
          <PriceLine label="TP2"          value={`$${v.trade_plan.tp2}`} valueCls="text-emerald-300" />
          {v.trade_plan.tp3 && <PriceLine label="TP3" value={`$${v.trade_plan.tp3}`} valueCls="text-emerald-300" />}
          <PriceLine label="Risk:Reward"  value={`1:${v.trade_plan.risk_reward}`} valueCls="text-amber-300 font-bold" />
          <PriceLine label="Lot Size"     value={`${v.trade_plan.lot_size ?? 0.01} (fixed)`} valueCls="text-blue-300" />
          <PriceLine label="Entry Type"   value={v.trade_plan.entry_type} valueCls="text-gray-300" />
          <PriceLine label="Invalidation" value={v.trade_plan.invalidation} />
        </Block>
      )}

      {/* 4-grid: Market State + Liquidity + Macro + Technical */}
      {v && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">

          <Block icon={Compass} title="Liquidity Model">
            <PriceLine label="Confirmed" value={v.liquidity_model?.confirmed ? 'YES' : 'NO'}
                       valueCls={v.liquidity_model?.confirmed ? 'text-emerald-400' : 'text-amber-400'} />
            <PriceLine label="Type"          value={v.liquidity_model?.type || '—'} />
            <PriceLine label="Swept level"   value={v.liquidity_model?.swept_level || '—'} />
            <PriceLine label="Target"        value={v.liquidity_model?.target_liquidity || '—'} />
          </Block>

          <Block icon={Activity} title="Macro Context">
            <PriceLine label="DXY bias"      value={v.macro_context?.dxy_bias} />
            <PriceLine label="Yields bias"   value={v.macro_context?.yields_bias} />
            <PriceLine label="News risk"     value={v.macro_context?.news_risk}
                       valueCls={v.macro_context?.news_risk === 'CLEAR' ? 'text-emerald-400' : 'text-amber-400'} />
            <PriceLine label="Gold bias"     value={v.macro_context?.gold_macro_bias} />
            <PriceLine label="Alignment"     value={v.macro_context?.macro_alignment}
                       valueCls={v.macro_context?.macro_alignment === 'Aligned' ? 'text-emerald-400' :
                                 v.macro_context?.macro_alignment === 'Conflicted' ? 'text-red-400' : 'text-gray-300'} />
          </Block>

          <Block icon={Activity} title="Technical Confirmation">
            <PriceLine label="Structure"  value={v.technical_confirmation?.structure} />
            <PriceLine label="EMA"        value={v.technical_confirmation?.ema} />
            <PriceLine label="VWAP"       value={v.technical_confirmation?.vwap} />
            <PriceLine label="RSI"        value={v.technical_confirmation?.rsi} />
            <PriceLine label="MACD"       value={v.technical_confirmation?.macd} />
            <PriceLine label="ATR / Vol"  value={v.technical_confirmation?.atr_volatility} />
          </Block>

          <Block icon={Lock} title="Execution Permission">
            <PriceLine label="Alert"           value={v.execution_permission?.allow_alert ? 'allowed' : 'denied'}
                       valueCls={v.execution_permission?.allow_alert ? 'text-emerald-400' : 'text-gray-500'} />
            <PriceLine label="Demo execution"  value={v.execution_permission?.allow_demo_execution ? 'allowed' : 'denied'}
                       valueCls={v.execution_permission?.allow_demo_execution ? 'text-emerald-400' : 'text-gray-500'} />
            <PriceLine label="Live execution"  value={v.execution_permission?.allow_live_execution ? 'allowed' : 'denied'}
                       valueCls={v.execution_permission?.allow_live_execution ? 'text-emerald-400' : 'text-gray-500'} />
            <div className="text-[10px] text-gray-400 mt-1 italic leading-snug">
              {v.execution_permission?.reason}
            </div>
          </Block>

        </div>
      )}

      {/* Key zones */}
      {v?.key_zones && (
        <Block icon={Shield} title="Key Reference Levels">
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-[10px]">
            <div>
              <div className="text-gray-500 uppercase tracking-widest mb-0.5">Resistance</div>
              {(v.key_zones.resistance || []).map((p, i) => <div key={i} className="font-mono text-red-300">${p}</div>)}
              {!v.key_zones.resistance?.length && <div className="text-gray-600">—</div>}
            </div>
            <div>
              <div className="text-gray-500 uppercase tracking-widest mb-0.5">Support</div>
              {(v.key_zones.support || []).map((p, i) => <div key={i} className="font-mono text-emerald-300">${p}</div>)}
              {!v.key_zones.support?.length && <div className="text-gray-600">—</div>}
            </div>
            <div>
              <div className="text-gray-500 uppercase tracking-widest mb-0.5">Today H/L</div>
              {(v.key_zones.immediate_supply || []).map((p, i) => <div key={'s'+i} className="font-mono text-amber-300">${p}</div>)}
              {(v.key_zones.immediate_demand || []).map((p, i) => <div key={'d'+i} className="font-mono text-amber-300">${p}</div>)}
            </div>
            <div className="col-span-2 md:col-span-3 mt-1 pt-1 border-t border-[#263044]">
              <div className="text-gray-500 uppercase tracking-widest mb-0.5">Round numbers (50-pt grid)</div>
              <div className="flex flex-wrap gap-2">
                {(v.key_zones.round_numbers || []).map((p, i) => (
                  <span key={i} className="font-mono text-gray-300 bg-black/20 px-1.5 py-0.5 rounded">${p}</span>
                ))}
              </div>
            </div>
          </div>
        </Block>
      )}

      {/* Management plan — only when there IS a plan */}
      {(dec === 'BUY' || dec === 'SELL' || dec === 'LONG' || dec === 'SHORT') && v?.management_plan && (
        <Block icon={Shield} title="Management Plan">
          <PriceLine label="After TP1"            value={v.management_plan.after_tp1} />
          <PriceLine label="After TP2"            value={v.management_plan.after_tp2} />
          <PriceLine label="Trail logic"          value={v.management_plan.trail_logic} />
          <PriceLine label="Early exit if…"       value={v.management_plan.early_exit_condition} />
        </Block>
      )}

      {/* Institutional logic footer */}
      {v?.institutional_logic && (
        <div className="text-[10px] text-gray-500 italic font-mono leading-tight">
          {v.institutional_logic}
        </div>
      )}

      {/* Footer */}
      <div className="flex items-center justify-between text-[10px] text-gray-600">
        <span>Polls every {POLL_MS / 1000}s · server cache 60s</span>
        <div className="flex items-center gap-2">
          {v?.timestamp && <span>Updated {formatKenyaTime(v.timestamp)} {KENYA_LABEL}</span>}
          <button onClick={forceRefresh} disabled={loading}
            className="flex items-center gap-1 px-2 py-0.5 rounded bg-[#161b27] border border-[#263044] hover:text-gray-300">
            <RefreshCw size={10} className={loading ? 'animate-spin' : ''} /> Refresh
          </button>
        </div>
      </div>
    </div>
  );
}
