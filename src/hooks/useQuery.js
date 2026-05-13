import { useState, useEffect, useCallback, useRef } from 'react';

/**
 * Generic data-fetching hook with loading state, error state, and mock fallback.
 *
 * @param {() => Promise<any>} fetchFn  - async function that returns data
 * @param {any[]}              deps     - values that trigger a re-fetch when changed
 * @param {any}                fallback - data to use when the fetch fails (mock fallback)
 *
 * @returns {{ data, loading, error, isUsingFallback, refetch }}
 */
export function useQuery(fetchFn, deps, fallback) {
  // Keep fetchFn in a ref so it's always current without being a dep
  const fnRef = useRef(fetchFn);
  fnRef.current = fetchFn;

  const [state, setState] = useState({
    data: fallback,
    loading: true,
    error: null,
    isUsingFallback: false,
  });

  const execute = useCallback(async () => {
    setState(prev => ({ ...prev, loading: true, error: null }));
    try {
      const data = await fnRef.current();
      setState({ data, loading: false, error: null, isUsingFallback: false });
    } catch (err) {
      const msg = err?.message ?? 'Unknown error';
      setState({ data: fallback, loading: false, error: msg, isUsingFallback: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    let cancelled = false;

    setState(prev => ({ ...prev, loading: true, error: null }));

    fnRef.current()
      .then(data => {
        if (!cancelled) setState({ data, loading: false, error: null, isUsingFallback: false });
      })
      .catch(err => {
        if (!cancelled) {
          const msg = err?.message ?? 'Unknown error';
          setState({ data: fallback, loading: false, error: msg, isUsingFallback: true });
        }
      });

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { ...state, refetch: execute };
}
