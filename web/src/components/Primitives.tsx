/**
 * The small pieces every screen is built from.
 *
 * They exist so the design rules in docs/DESIGN.md are enforced in one place rather than
 * remembered at each call site: a measured number and an estimated one cannot be made to look
 * alike, an interval cannot be dropped from a quality claim, and a disabled control cannot ship
 * without its reason.
 */
import type { ReactNode } from "react";
import { MARKS, MARK_LABELS } from "../lib/format";
import type { Interval, Provenance } from "../lib/types";

export function Mark({ kind }: { kind: Provenance }) {
  return (
    <abbr
      title={MARK_LABELS[kind]}
      className="no-underline text-graphite text-micro align-super ml-0.5"
      aria-label={MARK_LABELS[kind]}
    >
      {MARKS[kind]}
    </abbr>
  );
}

const TONE_CLASS: Record<string, string> = {
  measured: "text-ink font-medium",
  estimated: "text-graphite",
  projected: "text-graphite underline decoration-dotted underline-offset-2",
  simulated: "text-graphite",
};

/**
 * A number with its unit and its provenance. `kind` decides how it looks: measured values are
 * ink and medium weight, everything else is graphite, and projected values carry a dotted
 * underline as well.
 */
export function Figure({
  value,
  kind,
  size = "base",
  title,
}: {
  value: ReactNode;
  /** "none" for a structural fact — a count of what is on screen — that no mark describes. */
  kind: Provenance | "none";
  size?: "base" | "head" | "figure";
  title?: string;
}) {
  if (kind === "none") {
    const sizeClass =
      size === "figure" ? "text-figure" : size === "head" ? "text-head" : "";
    return (
      <span className={`text-ink font-medium ${sizeClass} whitespace-nowrap`} title={title}>
        {value}
      </span>
    );
  }
  const tone =
    kind === "provider" || kind === "exact"
      ? TONE_CLASS.measured
      : kind === "projected"
        ? TONE_CLASS.projected
        : TONE_CLASS.estimated;
  const sizeClass = size === "figure" ? "text-figure" : size === "head" ? "text-head" : "";
  return (
    <span className={`${tone} ${sizeClass} whitespace-nowrap`} title={title}>
      {value}
      <Mark kind={kind} />
    </span>
  );
}

/** A labelled metric with its interval. The interval is not optional. */
export function Metric({
  label,
  value,
  interval,
  kind,
  n,
  hint,
  size = "head",
}: {
  label: string;
  value: ReactNode;
  interval?: ReactNode;
  kind: Provenance | "none";
  n?: number;
  hint?: string;
  size?: "base" | "head" | "figure";
}) {
  return (
    <div className="min-w-0" title={hint}>
      <div className="text-small text-graphite">{label}</div>
      <div className="mt-0.5">
        <Figure value={value} kind={kind} size={size} title={hint} />
      </div>
      {interval !== undefined && (
        <div className="text-small text-graphite mt-0.5 tabular-nums">
          {interval}
          {n !== undefined && <span className="ml-2">n = {n.toLocaleString()}</span>}
        </div>
      )}
    </div>
  );
}

export function Legend({ kinds }: { kinds: Provenance[] }) {
  return (
    <div className="text-micro text-graphite flex flex-wrap gap-x-4 gap-y-1" data-testid="legend">
      {kinds.map((kind) => (
        <span key={kind}>
          <span className="text-ink">{MARKS[kind]}</span> {MARK_LABELS[kind]}
        </span>
      ))}
    </div>
  );
}

/** A section, separated by a hairline rule rather than wrapped in a card. */
export function Section({
  title,
  subtitle,
  right,
  children,
  id,
}: {
  title: string;
  subtitle?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section id={id} className="rule-t py-6">
      <header className="flex items-baseline justify-between gap-6 mb-3">
        <div className="min-w-0">
          <h2 className="text-head font-medium">{title}</h2>
          {subtitle && <p className="text-small text-graphite mt-0.5 max-w-prose">{subtitle}</p>}
        </div>
        {right && <div className="shrink-0">{right}</div>}
      </header>
      {children}
    </section>
  );
}

/**
 * A control that either does something real or is disabled with a one-line reason
 * (SPEC.md non-negotiable 9). The reason is required when disabled — the type enforces it.
 */
export function Button({
  children,
  onClick,
  disabledReason,
  variant = "default",
  testId,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabledReason?: string;
  variant?: "default" | "primary";
  testId?: string;
}) {
  const disabled = Boolean(disabledReason);
  const base =
    "px-3 py-1.5 text-base border transition-colors duration-100 disabled:cursor-not-allowed";
  const style =
    variant === "primary"
      ? "border-prussian bg-prussian text-chalk hover:bg-ink hover:border-ink disabled:bg-transparent disabled:text-graphite disabled:border-rule-strong"
      : "border-rule-strong text-ink hover:border-ink disabled:text-graphite disabled:border-rule";
  return (
    <span className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        className={`${base} ${style}`}
        data-testid={testId}
      >
        {children}
      </button>
      {disabled && (
        <span className="text-micro text-graphite max-w-[34ch] leading-snug" data-testid={testId ? `${testId}-reason` : undefined}>
          {disabledReason}
        </span>
      )}
    </span>
  );
}

export function Pill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "better" | "worse" | "neutral" | "scarce";
}) {
  const tones: Record<string, string> = {
    better: "border-verdigris text-verdigris",
    worse: "border-vermilion text-vermilion",
    neutral: "border-rule-strong text-graphite",
    scarce: "border-vermilion text-vermilion",
  };
  return (
    <span className={`inline-block border px-1.5 py-0.5 text-micro ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function LoadingRow({ what }: { what: string }) {
  return <p className="text-graphite text-base py-8">Computing {what} from the fixtures…</p>;
}

export function ErrorRow({ error, what }: { error: unknown; what: string }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="py-8">
      <p className="text-vermilion text-base">Could not load {what}.</p>
      <p className="text-graphite text-small mt-1 font-mono">{message}</p>
      <p className="text-graphite text-small mt-2">
        The engine builds every number from the committed fixtures. If they are missing, run{" "}
        <code className="font-mono">tokop build-test-fixtures</code>.
      </p>
    </div>
  );
}

/** A horizontal interval plot: the estimate, its range, and where the margin sits. */
export function IntervalPlot({
  interval,
  margin,
  domain,
}: {
  interval: Interval;
  margin: number;
  domain?: [number, number];
}) {
  const [lo, hi] = domain ?? [
    Math.min(interval.low, -margin) * 1.6,
    Math.max(interval.high, margin) * 1.6,
  ];
  const span = hi - lo || 1;
  const x = (v: number) => ((v - lo) / span) * 100;
  const clears = interval.low > -margin;
  const colour = clears ? "#1F6F5C" : interval.high < -margin ? "#C0452A" : "#5E6560";
  return (
    <figure className="mt-3" data-testid="interval-plot">
      <svg viewBox="0 0 100 26" className="w-full h-16" role="img" aria-label="Accuracy difference against the margin">
        <line x1="0" y1="13" x2="100" y2="13" stroke="#15181A1F" strokeWidth="0.3" />
        <line x1={x(0)} y1="4" x2={x(0)} y2="22" stroke="#15181A33" strokeWidth="0.4" />
        <line
          x1={x(-margin)}
          y1="2"
          x2={x(-margin)}
          y2="24"
          stroke="#C0452A"
          strokeWidth="0.6"
          strokeDasharray="1.5 1"
        />
        <line x1={x(interval.low)} y1="13" x2={x(interval.high)} y2="13" stroke={colour} strokeWidth="1.2" />
        <line x1={x(interval.low)} y1="9" x2={x(interval.low)} y2="17" stroke={colour} strokeWidth="0.8" />
        <line x1={x(interval.high)} y1="9" x2={x(interval.high)} y2="17" stroke={colour} strokeWidth="0.8" />
        <circle cx={x(interval.point)} cy="13" r="1.4" fill={colour} />
      </svg>
      <figcaption className="flex justify-between text-micro text-graphite tabular-nums -mt-3">
        <span>{(lo * 100).toFixed(0)}</span>
        <span className="text-vermilion">−{(margin * 100).toFixed(0)} pt margin</span>
        <span>{(hi * 100).toFixed(0)}</span>
      </figcaption>
    </figure>
  );
}
