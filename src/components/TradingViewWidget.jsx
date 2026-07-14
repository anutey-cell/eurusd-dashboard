import { useMemo, useState } from 'react';
import { BarChart2, Maximize2 } from 'lucide-react';

// TradingView chart via direct iframe embed.
//
// Previous attempts failed:
//   1. Legacy s3.tradingview.com/tv.js — deprecated, never sets window.TradingView
//   2. embed-widget-advanced-chart.js via createElement — script needs
//      document.currentScript which is null for dynamically-appended scripts
//   3. dangerouslySetInnerHTML with script tag — browsers don't execute
//      script tags inserted via innerHTML
//
// The direct iframe is what TradingView's own widget script generates
// internally. It works on any origin, needs no JS bootstrap, and TV serves
// candles/indicators from their own servers.

const TIMEFRAMES = [
  { label: 'M15', value: '15'  },
  { label: 'H1',  value: '60'  },
  { label: 'H4',  value: '240' },
  { label: 'D1',  value: 'D'   },
];

export default function TradingViewWidget({ symbol = 'OANDA:XAUUSD' }) {
  const [interval, setInterval_] = useState('60');

  const iframeSrc = useMemo(() => {
    const params = new URLSearchParams({
      symbol,
      interval,
      hidesidetoolbar: '0',
      symboledit:      '0',
      saveimage:       '0',
      toolbarbg:       '161b27',
      studies:         'RSI@tv-basicstudies,MACD@tv-basicstudies',
      theme:           'dark',
      style:           '1',
      timezone:        'Africa/Nairobi',
      hideideas:       '1',
      locale:          'en',
      withdateranges:  '1',
    });
    return `https://s.tradingview.com/widgetembed/?${params.toString()}`;
  }, [symbol, interval]);

  return (
    <div className="card flex flex-col h-full min-h-[480px]">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <BarChart2 size={13} className="text-blue-400" />
          <span className="card-title">XAU/USD Chart</span>
        </div>
        <div className="flex items-center gap-2">
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

      <div className="relative flex-1" style={{ minHeight: 420 }}>
        <iframe
          key={iframeSrc}
          src={iframeSrc}
          title="TradingView XAU/USD Chart"
          allowFullScreen
          className="w-full h-full block border-0"
          style={{ minHeight: 420, background: '#0d1117' }}
        />
      </div>
    </div>
  );
}
