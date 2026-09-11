/**
 * Formatting, and the rule that shapes the whole interface: a measured number and an estimated
 * number must never look alike (docs/DESIGN.md section 3).
 *
 * Every figure ships with its unit, and every figure carries a provenance mark defined once in
 * the legend. Nothing here invents a number — these functions only render what the engine
 * computed.
 */
import type { Interval, Provenance } from "./types";

/** Provenance marks, defined once. The legend renders this same map. */
export const MARKS: Record<Provenance, string> = {
  provider: "▪",
  exact: "=",
  estimated: "≈",
  projected: "→",
  simulated: "~",
};

export const MARK_LABELS: Record<Provenance, string> = {
  provider: "provider-reported",
  exact: "exact count",
  estimated: "estimated",
  projected: "projected",
  simulated: "simulated",
};

export function usd(value: number | string, digits = 5): string {
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  return `$${n.toFixed(digits)}`;
}

export function usdShort(value: number | string): string {
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1) return `$${n.toFixed(2)}`;
  if (Math.abs(n) >= 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(5)}`;
}

export function pct(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function points(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return "—";
  const scaled = value * 100;
  return `${scaled >= 0 ? "+" : ""}${scaled.toFixed(digits)}`;
}

export function tokens(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${Math.round(value).toLocaleString()} tok`;
}

export function count(value: number): string {
  return Math.round(value).toLocaleString();
}

export function ms(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.round(value)} ms`;
}

/** An interval as a range. A quality claim without one is a bug (DESIGN.md section 3). */
export function intervalPct(interval: Interval, digits = 1): string {
  return `${(interval.low * 100).toFixed(digits)}–${(interval.high * 100).toFixed(digits)}`;
}

export function intervalUsd(interval: Interval, digits = 5): string {
  return `${usd(interval.low, digits)}–${usd(interval.high, digits)}`;
}

export function intervalPoints(interval: Interval, digits = 1): string {
  return `[${points(interval.low, digits)}, ${points(interval.high, digits)}]`;
}

/** The provenance a report's numbers carry, which depends on whether the fixtures are real. */
export function measuredMark(isTestData: boolean): Provenance {
  return isTestData ? "simulated" : "provider";
}

export const VERDICT_TONE: Record<string, "better" | "worse" | "neutral"> = {
  non_inferior: "better",
  worse: "worse",
  inconclusive: "neutral",
};

/** Tier colour by position: a lightness ramp on prussian, never a hue per tier. */
export function tierTint(index: number, total: number): string {
  const steps = Math.max(1, total - 1);
  const alpha = 0.25 + (0.75 * index) / steps;
  return `rgba(18, 69, 107, ${alpha.toFixed(2)})`;
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
