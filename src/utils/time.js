/**
 * East Africa Time (EAT) helpers.
 *
 * Backend stamps everything in UTC (canonical for trading). The UI displays
 * Kenyan time (Africa/Nairobi, UTC+3, no DST) for the operator.
 *
 * All helpers accept either a Date or any value parseable by `new Date(...)`.
 */
export const KENYA_TZ     = 'Africa/Nairobi';
export const KENYA_OFFSET = 3;        // hours ahead of UTC, year-round (no DST)
export const KENYA_LABEL  = 'EAT';

function toDate(d) {
  if (d instanceof Date) return d;
  if (d == null) return new Date();
  return new Date(d);
}

/** "HH:MM:SS" in EAT, 24h. */
export function formatKenyaTime(d) {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: KENYA_TZ,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).format(toDate(d));
}

/** "HH:MM" in EAT, 24h. */
export function formatKenyaHM(d) {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: KENYA_TZ,
    hour: '2-digit', minute: '2-digit',
    hour12: false,
  }).format(toDate(d));
}

/** "20 May 11:45" in EAT — compact date+time. */
export function formatKenyaDateTime(d) {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: KENYA_TZ,
    day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit',
    hour12: false,
  }).format(toDate(d));
}

/** Map UTC hour (0-23) → EAT hour (UTC+3, no DST). */
export function utcHourToKenya(h) {
  return ((Number(h) % 24) + KENYA_OFFSET + 24) % 24;
}

/** Map EAT hour (0-23) → UTC hour. */
export function kenyaHourToUtc(h) {
  return ((Number(h) % 24) - KENYA_OFFSET + 24) % 24;
}

/**
 * Convert a UTC window string like "07:00-10:00" to its EAT equivalent
 * "10:00-13:00". Returns the original string if it doesn't match the pattern.
 */
export function utcWindowToKenya(window) {
  if (!window) return window;
  const m = String(window).match(/^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/);
  if (!m) return window;
  const [, sh, sm, eh, em] = m;
  const sk = String(utcHourToKenya(sh)).padStart(2, '0');
  const ek = String(utcHourToKenya(eh)).padStart(2, '0');
  return `${sk}:${sm}-${ek}:${em}`;
}

/** Current EAT hour (0-23). */
export function nowKenyaHour() {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: KENYA_TZ, hour: '2-digit', hour12: false,
  }).formatToParts(new Date());
  const h = parts.find(p => p.type === 'hour')?.value;
  return Number(h ?? 0);
}
