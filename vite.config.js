import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',   // required for Docker / Codespaces / remote access
    port: 5173,
    // Vite ≥4.6 blocks requests whose Host header isn't in this list.
    // The dashboard is served behind Caddy on a DuckDNS subdomain — without
    // this whitelist, every browser request returns 403 "Blocked request".
    // We trust Caddy to be the only ingress; setting this to true disables
    // the host check (HMR security is irrelevant in container/prod context).
    allowedHosts: true,
  },
});
