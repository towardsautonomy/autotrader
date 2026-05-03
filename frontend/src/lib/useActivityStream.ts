"use client";

import { useEffect, useRef } from "react";
import { activityStreamUrl, ActivityEvent } from "@/lib/api";

/**
 * Subscribe to the server's SSE activity bus and run `onRefresh` whenever an
 * event whose `type` is in `topics` arrives. Reloads are debounced (default
 * 300ms) so a burst of events triggers a single reload.
 *
 * Pages use this to get broker-platform snappiness: reacting within a second
 * of an order fill / close, instead of waiting for the next poll tick.
 */
export function useActivityStream(
  topics: readonly string[],
  onRefresh: () => void,
  { debounceMs = 300 }: { debounceMs?: number } = {},
): void {
  const topicSet = useRef<Set<string>>(new Set(topics));
  const refreshRef = useRef(onRefresh);

  useEffect(() => {
    topicSet.current = new Set(topics);
  }, [topics]);
  useEffect(() => {
    refreshRef.current = onRefresh;
  }, [onRefresh]);

  useEffect(() => {
    const es = new EventSource(activityStreamUrl());
    let pending: ReturnType<typeof setTimeout> | null = null;
    const schedule = () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        refreshRef.current();
      }, debounceMs);
    };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as ActivityEvent;
        if (!data.type) return;
        if (topicSet.current.has(data.type)) schedule();
      } catch {
        /* ignore malformed */
      }
    };
    return () => {
      if (pending) clearTimeout(pending);
      es.close();
    };
  }, [debounceMs]);
}
