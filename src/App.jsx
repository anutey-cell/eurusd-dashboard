import { useState } from 'react';
import Header from './components/Header';
import BackendBanner from './components/BackendBanner';
import Dashboard from './components/Dashboard';

const PAIR_OPTIONS = [
  { code: 'eurusd', label: 'EUR/USD', symbol: 'FX:EURUSD',      decimals: 5 },
  { code: 'xauusd', label: 'XAU/USD', symbol: 'OANDA:XAU_USD',  decimals: 2 },
];

export default function App() {
  const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
  const [selectedPair, setSelectedPair] = useState(PAIR_OPTIONS[0]);

  return (
    <div className="min-h-screen bg-[#0d1117] flex flex-col">
      <Header selectedPair={selectedPair} />
      <BackendBanner />
      <Dashboard selectedPair={selectedPair} onPairChange={setSelectedPair} />

      <footer className="border-t border-[#263044] px-4 py-2 flex items-center justify-between text-[10px] text-gray-700 flex-shrink-0">
        <span>
          FX Signal Pro — Phase 9 · API:{' '}
          <span className="font-mono">{apiBase}/api/v1</span>
        </span>
        <span>Phase 10: Multi-pair · COT data · MT5 journal sync</span>
      </footer>
    </div>
  );
}
