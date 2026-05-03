"use client";

import { useEffect, useRef } from "react";

/**
 * Trigger `refresh` when the tab regains focus / visibility / network.
 *
 * Mobile browsers (iOS Safari especially) suspend JS in background tabs
 * and kill in-flight fetches on cold app open. Without this, pages look
 * empty until the user pulls-to-refresh. With this, the page refetches
 * the moment the tab is foregrounded or the device comes back online.
 */
export function useRefreshOnResume(refresh: () => void): void {
  const ref = useRef(refresh);
  useEffect(() => {
    ref.current = refresh;
  }, [refresh]);

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "visible") ref.current();
    };
    const onFocus = () => ref.current();
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", onFocus);
    window.addEventListener("online", onFocus);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("online", onFocus);
    };
  }, []);
}
