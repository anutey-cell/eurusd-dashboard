import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Hard-block requests for sensitive paths at middleware level. Vite's
// server.fs.deny does not always short-circuit before the import-analyzer,
// which crashed the HMR overlay when a bot probed /.git/index. Middleware
// returns 403 unconditionally for anything under .git/, .env*, or
// backend/deploy/logs/mt5_bridge/pycache, before Vite ever tries to parse it.
const SENSITIVE_PATH_RE = /(^|\/)(\.git|\.env|backend|deploy|mt5_bridge|logs|__pycache__)(\/|$)/i;
const blockSensitivePaths = {
  name: 'block-sensitive-paths',
  configureServer(server) {
    server.middlewares.use((req, res, next) => {
      if (req.url && SENSITIVE_PATH_RE.test(req.url)) {
        res.statusCode = 403;
        res.setHeader('Content-Type', 'text/plain');
        res.end('Forbidden');
        return;
      }
      next();
    });
  },
};

export default defineConfig({
  plugins: [blockSensitivePaths, react()],
  server: {
    host: '0.0.0.0',   // required for Docker / Codespaces / remote access
    port: 5173,
    // Vite ≥4.6 blocks requests whose Host header isn't in this list.
    // The dashboard is served behind Caddy on a DuckDNS subdomain — without
    // this whitelist, every browser request returns 403 "Blocked request".
    // We trust Caddy to be the only ingress; setting this to true disables
    // the host check (HMR security is irrelevant in container/prod context).
    allowedHosts: true,
    // File-serve deny list. Vite's dev server serves the container's project
    // root by default, which includes .git/, .env*, backend/, deploy/, etc.
    // A public request for /app/.git/index (common bot probe) was hitting
    // Vite's import-analyzer, which then crashed trying to parse the binary
    // git-index file as JavaScript AND was serving the git tree publicly.
    // Deny everything sensitive so unrelated paths return 403 instead of
    // both crashing HMR and leaking source.
    fs: {
      deny: [
        '.git/**',
        '.env',
        '.env.*',
        '*.pem',
        '*.crt',
        '*.key',
        'backend/**',
        'mt5_bridge/**',
        'deploy/**',
        'logs/**',
        '**/__pycache__/**',
      ],
    },
  },
});
