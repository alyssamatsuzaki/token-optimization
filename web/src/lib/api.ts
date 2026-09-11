import { useQuery } from "@tanstack/react-query";
import type {
  BriefResult,
  DeleteResult,
  RunsView,
  CompareExamples,
  Health,
  InspectResult,
  PipelineTemplate,
  Report,
  SettingsView,
  SpendView,
  Trace,
} from "./types";

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

export interface InspectBody {
  system: string;
  user: string;
  tools: string;
  max_tokens: number;
  cache_after_system: boolean;
  apply_fixes: boolean;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${path} returned ${response.status}: ${detail.slice(0, 300)}`);
  }
  return (await response.json()) as T;
}

export function inspectPrompt(body: InspectBody) {
  return post<InspectResult>("/api/inspect", body);
}

export function usePipelineTemplate(pipelineId: string) {
  return useQuery({
    queryKey: ["pipeline-template", pipelineId],
    queryFn: () => get<PipelineTemplate>(`/api/inspect/pipeline/${pipelineId}`),
  });
}

export function useCompare() {
  return useQuery({ queryKey: ["compare"], queryFn: () => get<CompareExamples>("/api/compare") });
}

export function useBrief() {
  return useQuery({ queryKey: ["brief"], queryFn: () => get<BriefResult>("/api/brief") });
}

export function useRuns() {
  return useQuery({ queryKey: ["runs"], queryFn: () => get<RunsView>("/api/runs") });
}

export function deleteRunContent(runId: string) {
  return post<DeleteResult>(`/api/runs/${runId}/content`, {});
}

export function storePreference(compareSessionId: string, model: string) {
  return post<{ stored: boolean; model: string; note: string }>("/api/preferences", {
    compare_session_id: compareSessionId,
    model,
  });
}

export function useSettings() {
  return useQuery({ queryKey: ["settings"], queryFn: () => get<SettingsView>("/api/settings") });
}

export function useSpend() {
  return useQuery({ queryKey: ["spend"], queryFn: () => get<SpendView>("/api/spend") });
}
