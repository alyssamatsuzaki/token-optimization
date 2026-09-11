import { useQuery } from "@tanstack/react-query";
import type { Health, Report, Trace } from "./types";

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${path} returned ${response.status}: ${detail.slice(0, 300)}`);
  }
  return (await response.json()) as T;
}

export function useHealth() {
  return useQuery({ queryKey: ["health"], queryFn: () => get<Health>("/api/health") });
}

export function useReport(protectScarce: boolean) {
  return useQuery({
    queryKey: ["report", protectScarce],
    queryFn: () => get<Report>(`/api/report?protect_scarce=${protectScarce}`),
    staleTime: Infinity,
  });
}

export function useTrace(taskId: string | null) {
  return useQuery({
    queryKey: ["trace", taskId],
    queryFn: () => get<Trace>(`/api/trace/${taskId}`),
    enabled: taskId !== null,
  });
}

export function useSettings() {
  return useQuery({ queryKey: ["settings"], queryFn: () => get<Record<string, unknown>>("/api/settings") });
}

export function useSpend() {
  return useQuery({ queryKey: ["spend"], queryFn: () => get<Record<string, unknown>>("/api/spend") });
}
