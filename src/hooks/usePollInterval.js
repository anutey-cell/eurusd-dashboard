/**
 * usePollInterval — visibility-aware polling hook
 * =================================================
 *
 * Replaces:
 *   useEffect(() => {
 *     load();
 *     const id = setInterval(load, POLL_MS);
 *     return () => clearInterval(id);
 *   }, [load]);
 *
 * With:
 *   usePollInterval(load, POLL_MS);
 *
 * Benefits:
 *   1. Pauses polling when the browser tab is hidden (Page Visibility API).
 *      Prevents the dashboard from hammering the backend / TradingView while
 *      the user has it backgrounded for hours.
 *   2. Re-polls immediately when the tab becomes visible again, so the user
 *      sees fresh data on return without waiting one full interval.
 *   3. Single timer cleanup — no leaked intervals between tab switches.
 *   4. Optional `enabled` flag for disabling without unmounting.
 *
 * Pre-existing panels each had their own poll loop running every 15-60 s,
 * regardless of visibility. With 8+ panels mounted, that's a poll storm.
 * This hook cuts cold-tab traffic to zero.
 *
 * Usage:
 *   import { usePollInterval } from '../hooks/usePollInterval';
 *   ...
 *   usePollInterval(load, 30_000);                  // basic
 *   usePollInterval(load, 60_000, { enabled: !!status });   // gated
 */
import { useEffect, useRef } from 'react';

export function usePollInterval(fn, intervalMs, { enabled = true, runOnMount = true } = {}) {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return undefined;

    let cancelled  = false;
    let timeoutId  = null;
    const safeCall = () => {
      try { fnRef.current?.(); } catch (e) { /* swallow */ }
    };

    function schedule() {
      if (cancelled) return;
      timeoutId = setTimeout(() => {
        if (cancelled) return;
        if (typeof document !== 'undefined' && document.hidden) {
          // Tab hidden — re-check in a few seconds without firing the poll
          schedule();
          return;
        }
        safeCall();
        schedule();
      }, intervalMs);
    }

    if (runOnMount) safeCall();
    schedule();

    function onVisibilityChange() {
      if (cancelled) return;
      if (!document.hidden) {
        // Just became visible — fire immediately so user sees fresh data
        safeCall();
      }
    }

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibilityChange);
    }

    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibilityChange);
      }
    };
  }, [enabled, intervalMs, runOnMount]);
}
