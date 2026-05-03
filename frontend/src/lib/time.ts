// All user-facing timestamps render in America/Los_Angeles.
// Browsers in other zones still show PT — the wall clock trading cares about.

const TZ = "America/Los_Angeles";

type DateInput = string | number | Date | null | undefined;

function toDate(v: DateInput): Date | null {
  if (v == null) return null;
  const d = v instanceof Date ? v : new Date(v);
  return isNaN(d.getTime()) ? null : d;
}

export function fmtDateTime(v: DateInput, fallback = "—"): string {
  const d = toDate(v);
  if (!d) return fallback;
  return d.toLocaleString(undefined, { hour12: false, timeZone: TZ });
}

export function fmtTime(v: DateInput, fallback = "—"): string {
  const d = toDate(v);
  if (!d) return fallback;
  return d.toLocaleTimeString(undefined, { hour12: false, timeZone: TZ });
}

export function fmtTimeHM(v: DateInput, fallback = "—"): string {
  const d = toDate(v);
  if (!d) return fallback;
  return d.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    timeZone: TZ,
  });
}

export function fmtDate(v: DateInput, fallback = "—"): string {
  const d = toDate(v);
  if (!d) return fallback;
  return d.toLocaleDateString(undefined, { timeZone: TZ });
}

export const TZ_LABEL = "PT";
