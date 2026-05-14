import Header from './components/Header';
import BackendBanner from './components/BackendBanner';
import Dashboard from './components/Dashboard';

// XAU/USD is the only supported instrument.
const INSTRUMENT = { code: 'xauusd', label: 'XAU/USD', symbol: 'OANDA:XAUUSD', decimals: 2 };

export default function App() {
  const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

  return (
    <div className="min-h-screen bg-[#0d1117] flex flex-col">
      <Header instrument={INSTRUMENT} />
      <BackendBanner />
      <Dashboard instrument={INSTRUMENT} />

      <footer className="border-t border-[#263044] px-4 py-2 flex items-center justify-between text-[10px] text-gray-700 flex-shrink-0">
        <span>
          XAU/USD Signal Dashboard · API:{' '}
          <span className="font-mono">{apiBase}/api/v1</span>
        </span>
        <span>Instrument: XAU/USD · 50-point target · Manual confirmation only · Execution disabled</span>
      </footer>
    </div>
  );
}
