import { useEffect, useRef, useState } from 'react';
import { BarChart2, Maximize2 } from 'lucide-react';

const TIMEFRAMES = [
  { label: 'M15', value: '15'  },
  { label: 'H1',  value: '60'  },
  { label: 'H4',  value: '240' },
  { label: 'D1',  value: 'D'   },
];

// XAU/USD chart — symbol candidates: OANDA:XAUUSD → TVC:GOLD → FOREXCOM:XAUUSD
export default function TradingViewWidget({ symbol = 'OANDA:XAUUSD' }) {
  const containerRef = useRef(null);
  const [interval, setInterval_] = useState('240');
  const [loaded, setLoaded] = useState(false);
  const widgetRef = useRef(null);

  useEffect(() => {
    setLoaded(false);

    // Remove any previous widget
    if (containerRef.current) containerRef.current.innerHTML = '';

    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/tv.js';
    script.async = true;
    script.onload = () => {
      if (!containerRef.current) return;
      widgetRef.current = new window.TradingView.widget({
        autosize: true,
        symbol: symbol,
        interval,
        timezone: 'Etc/UTC',
        theme: 'dark',
        style: '1',
        locale: 'en',
        toolbar_bg: '#161b27',
        enable_publishing: false,
        hide_side_toolbar: false,
        allow_symbol_change: false,
        container_id: 'tv_chart_container',
        studies: ['RSI@tv-basicstudies', 'MACD@tv-basicstudies'],
        overrides: {
          'paneProperties.background': '#0d1117',
          'paneProperties.backgroundType': 'solid',
          'scalesProperties.textColor': '#6b7280',
          'mainSeriesProperties.candleStyle.upColor': '#10b981',
          'mainSeriesProperties.candleStyle.downColor': '#ef4444',
          'mainSeriesProperties.candleStyle.borderUpColor': '#10b981',
          'mainSeriesProperties.candleStyle.borderDownColor': '#ef4444',
          'mainSeriesProperties.candleStyle.wickUpColor': '#10b981',
          'mainSeriesProperties.candleStyle.wickDownColor': '#ef4444',
        },
      });
      setLoaded(true);
    };

    // Prevent duplicate script tags
    const existing = document.querySelector('script[src="https://s3.tradingview.com/tv.js"]');
    if (existing) {
      if (window.TradingView) {
        script.onload();
      }
      return;
    }

    document.head.appendChild(script);
    return () => {
      if (containerRef.current) containerRef.current.innerHTML = '';
    };
  }, [interval, symbol]);

  return (
    <div className="card flex flex-col h-full min-h-[480px]">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <BarChart2 size={13} className="text-blue-400" />
          <span className="card-title">XAU/USD Chart</span>
        </div>
        <div className="flex items-center gap-2">
          {/* Timeframe buttons */}
          <div className="flex bg-[#0d1117] rounded-lg border border-[#263044] p-0.5 gap-0.5">
            {TIMEFRAMES.map(tf => (
              <button
                key={tf.value}
                onClick={() => setInterval_(tf.value)}
                className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${
                  interval === tf.value
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-400 hover:text-white'
                }`}
              >
                {tf.label}
              </button>
            ))}
          </div>
          <Maximize2 size={12} className="text-gray-600 cursor-pointer hover:text-gray-300 transition-colors" />
        </div>
      </div>

      {/* Chart container */}
      <div className="relative flex-1">
        {!loaded && (
          <div className="absolute inset-0 flex flex-col items-center justify-center bg-[#161b27] z-10 gap-3">
            <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            <span className="text-xs text-gray-500">Loading TradingView chart…</span>
          </div>
        )}
        <div
          id="tv_chart_container"
          ref={containerRef}
          className="w-full h-full"
          style={{ minHeight: 420 }}
        />
      </div>
    </div>
  );
}
