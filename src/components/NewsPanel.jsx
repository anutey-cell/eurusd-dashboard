import { useState, useEffect } from 'react';
import { Newspaper, Clock, TrendingUp, TrendingDown, Minus, RefreshCw } from 'lucide-react';
import { newsItems as mockNewsItems, recentNews as mockRecentNews } from '../data/mockData';
import { SkeletonCard } from './Skeleton';
import { FallbackChip } from './BackendBanner';

const IMPACT_CONFIG = {
  high:   { text: 'text-red-400',   label: 'HIGH', border: 'border-red-500'   },
  medium: { text: 'text-amber-400', label: 'MED',  border: 'border-amber-500' },
  low:    { text: 'text-blue-400',  label: 'LOW',  border: 'border-blue-500'  },
};

const SENTIMENT_ICON = {
  bullish: <TrendingUp  size={12} className="text-emerald-400" />,
  bearish: <TrendingDown size={12} className="text-red-400"    />,
  neutral: <Minus        size={12} className="text-gray-500"   />,
};

function useNextEventCountdown(events) {
  const [label, setLabel] = useState('');
  useEffect(() => {
    const tick = () => {
      const upcoming = events
        .filter(e => e.pending)
        .map(e => ({ ...e, ms: new Date(e.time) - Date.now() }))
        .filter(e => e.ms > 0)
        .sort((a, b) => a.ms - b.ms);
      if (!upcoming.length) { setLabel('No upcoming events'); return; }
      const { ms, event } = upcoming[0];
      const h = Math.floor(ms / 3600000);
      const m = Math.floor((ms % 3600000) / 60000);
      const s = Math.floor((ms % 60000) / 1000);
      setLabel(`Next: ${event} in ${h}h ${m}m ${s}s`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [events]);
  return label;
}

// Props supplied by Dashboard; mock defaults make component work standalone.
export default function NewsPanel({
  loading         = false,
  error           = null,
  isUsingFallback = false,
  refetch         = null,
  newsItems       = mockNewsItems,
  recentNews      = mockRecentNews,
}) {
  const [tab, setTab] = useState('calendar');
  const countdown = useNextEventCountdown(newsItems ?? mockNewsItems);

  const formatTime = iso => new Date(iso).toUTCString().slice(17, 22);

  const items = newsItems ?? mockNewsItems;
  const news  = recentNews ?? mockRecentNews;

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <Newspaper size={13} className="text-blue-400" />
          <span className="card-title">News &amp; Calendar</span>
          <FallbackChip isUsingFallback={isUsingFallback} error={error} />
        </div>
        <div className="flex items-center gap-2">
          {refetch && (
            <button onClick={refetch} title="Refresh" className="text-gray-600 hover:text-gray-400 transition-colors">
              <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
            </button>
          )}
          <div className="flex bg-[#0d1117] rounded-lg border border-[#263044] p-0.5">
            {[['calendar', 'Calendar'], ['news', 'News']].map(([key, lbl]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`px-2.5 py-0.5 text-[10px] font-medium rounded-md transition-colors ${
                  tab === key ? 'bg-[#263044] text-white' : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {lbl}
              </button>
            ))}
          </div>
        </div>
      </div>

      {loading ? (
        <SkeletonCard rows={5} />
      ) : tab === 'calendar' ? (
        <div className="flex flex-col p-4 gap-3">
          <div className="flex items-center gap-2 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
            <Clock size={11} className="text-amber-400 flex-shrink-0" />
            <span className="text-[11px] text-amber-300 font-medium">{countdown}</span>
          </div>
          <div className="space-y-2">
            {items.map(ev => {
              const cfg = IMPACT_CONFIG[ev.impact] ?? IMPACT_CONFIG.low;
              return (
                <div key={ev.id} className={`bg-[#1e2535] rounded-lg p-3 border-l-2 ${cfg.border}`}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 mb-0.5">
                        <span className="text-[10px] font-bold text-gray-500">{formatTime(ev.time)} UTC</span>
                        <span className={`text-[9px] font-bold px-1 rounded ${cfg.text}`}>{cfg.label}</span>
                        <span className="text-[10px] font-bold text-blue-400">{ev.currency}</span>
                        {ev.pending && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />}
                      </div>
                      <div className="text-xs text-gray-200 font-medium truncate">{ev.event}</div>
                    </div>
                    {!ev.pending && ev.actual && (
                      <div className={`text-right flex-shrink-0 ${ev.beat ? 'text-emerald-400' : 'text-red-400'}`}>
                        <div className="text-[10px] font-mono font-bold">{ev.actual}</div>
                        <div className="text-[9px] text-gray-600">actual</div>
                      </div>
                    )}
                  </div>
                  <div className="flex gap-4 mt-1.5 text-[10px] text-gray-600">
                    <span>Fcst: <span className="text-gray-400">{ev.forecast}</span></span>
                    <span>Prev: <span className="text-gray-400">{ev.previous}</span></span>
                    {ev.pending && <span className="text-amber-400/70">Pending</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="p-4 space-y-2">
          {news.map(n => (
            <div key={n.id} className="bg-[#1e2535] rounded-lg p-3 flex gap-2.5">
              <div className="flex-shrink-0 mt-0.5">{SENTIMENT_ICON[n.sentiment] ?? SENTIMENT_ICON.neutral}</div>
              <div className="flex-1 min-w-0">
                <p className="text-xs text-gray-300 leading-relaxed">{n.headline}</p>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-[10px] text-gray-600 font-mono">{n.time}</span>
                  <span className="text-[10px] text-blue-500">{n.source}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
