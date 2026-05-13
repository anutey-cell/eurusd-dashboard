import Header from './components/Header';
import BackendBanner from './components/BackendBanner';
import Dashboard from './components/Dashboard';

export default function App() {
  const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

  return (
    <div className="min-h-screen bg-[#0d1117] flex flex-col">
      <Header />
      <BackendBanner />
      <Dashboard />

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
