/*
 * Health polling (spec §21.24 *Health*): auto-refresh every 30 seconds while
 * visible, and stop when the page is hidden. An admin screen left open on a
 * background tab should not keep a Pi busy answering it, and the §6.4 rule
 * that presence is what holds a session applies to work the page causes too.
 */
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "@/api/client";

import type { Health } from "./types";

export const HEALTH_REFRESH_MS = 30_000;

export const healthKey = ["system", "health"] as const;

/** True while the document is visible. Drives the poll, nothing else. */
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => (typeof document === "undefined" ? true : !document.hidden));
  useEffect(() => {
    const onChange = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

export function useHealth(): UseQueryResult<Health> {
  const visible = useDocumentVisible();
  const client = useQueryClient();
  const query = useQuery({ queryKey: healthKey, queryFn: () => api<Health>("/system/health") });

  useEffect(() => {
    if (!visible) return undefined;
    const timer = setInterval(() => {
      void client.invalidateQueries({ queryKey: healthKey });
    }, HEALTH_REFRESH_MS);
    return () => clearInterval(timer);
  }, [visible, client]);

  return query;
}
