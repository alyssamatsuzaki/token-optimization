/**
 * The cost-quality frontier: the one place the design spends its boldness
 * (docs/DESIGN.md section 4).
 *
 * Every threshold setting the search evaluated, scored on the test split from recorded
 * responses. The operating point is drawn as a crosshair with its coordinates labelled, and the
 * note under the chart says it was chosen on calibration before any of these test numbers
 * existed — which is the only reason they are worth reading.
 */
import { useMemo, useState } from "react";
import { pct, usd } from "../lib/format";
import type { FrontierPointView, WaterfallStepView } from "../lib/types";

interface Marker {
  label: string;
  cost: number;
  accuracy: number;
}

export function FrontierChart({
  points,
  markers,
  operatingNote,
}: {
  points: FrontierPointView[];
  markers: WaterfallStepView[];
  operatingNote: string;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const data = useMemo(
    () => points.map((p) => ({ ...p, cost: Number(p.cost_per_task_usd) })),
    [points],
  );
  const marks: Marker[] = useMemo(
    () =>
      markers
        .filter((m) => m.pipeline !== "B3")
        .map((m) => ({
          label: m.pipeline,
          cost: Number(m.cost_per_task_usd),
          accuracy: m.accuracy.point,
        })),
    [markers],
  );

  const operating = data.find((p) => p.is_operating_point);
  const allCosts = [...data.map((d) => d.cost), ...marks.map((m) => m.cost)];
  const allAcc = [...data.map((d) => d.accuracy), ...marks.map((m) => m.accuracy)];
  if (!allCosts.length) return null;

  const padX = 0.08;
  const minCost = Math.min(...allCosts) * (1 - padX);
  const maxCost = Math.max(...allCosts) * (1 + padX);
  const minAcc = Math.min(...allAcc) - 0.02;
  const maxAcc = Math.min(1, Math.max(...allAcc) + 0.015);

  const W = 720;
  const H = 320;
  const M = { top: 16, right: 96, bottom: 44, left: 56 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  const sx = (c: number) => M.left + ((c - minCost) / (maxCost - minCost || 1)) * plotW;
  const sy = (a: number) => M.top + plotH - ((a - minAcc) / (maxAcc - minAcc || 1)) * plotH;

  const costTicks = 5;
  const accTicks = 4;

  return (
    <figure className="bg-chalk border border-rule p-4" data-testid="frontier-chart">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto"
        role="img"
        aria-label="Cost against accuracy for every evaluated cascade threshold setting"
      >
        {Array.from({ length: accTicks + 1 }, (_, i) => {
          const a = minAcc + ((maxAcc - minAcc) * i) / accTicks;
          return (
            <g key={`a${i}`}>
              <line x1={M.left} y1={sy(a)} x2={W - M.right} y2={sy(a)} stroke="#15181A14" strokeWidth="1" />
              <text x={M.left - 8} y={sy(a) + 3.5} textAnchor="end" fontSize="11" fill="#5E6560">
                {(a * 100).toFixed(0)}%
              </text>
            </g>
          );
        })}
        {Array.from({ length: costTicks + 1 }, (_, i) => {
          const c = minCost + ((maxCost - minCost) * i) / costTicks;
          return (
            <text key={`c${i}`} x={sx(c)} y={H - M.bottom + 16} textAnchor="middle" fontSize="11" fill="#5E6560">
              ${c.toFixed(4)}
            </text>
          );
        })}

        {/* Every evaluated setting. Muted, because these are simulated from recorded answers. */}
        {data.map((d, i) => (
          <circle
            key={i}
            cx={sx(d.cost)}
            cy={sy(d.accuracy)}
            r={hover === i ? 4 : 2.2}
            fill="#12456B"
            fillOpacity={d.is_operating_point ? 1 : 0.28}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          />
        ))}

        {/* Baseline pipelines, directly labelled: the chart never relies on hue alone. */}
        {marks.map((m) => (
          <g key={m.label}>
            <path
              d={`M${sx(m.cost) - 4},${sy(m.accuracy)} L${sx(m.cost)},${sy(m.accuracy) - 4} L${sx(m.cost) + 4},${sy(m.accuracy)} L${sx(m.cost)},${sy(m.accuracy) + 4} Z`}
              fill="#15181A"
            />
            <text x={sx(m.cost) + 8} y={sy(m.accuracy) - 6} fontSize="12" fill="#15181A" fontWeight="500">
              {m.label}
            </text>
          </g>
        ))}

        {/* The operating point, as a crosshair with its coordinates. */}
        {operating && (
          <g data-testid="operating-point">
            <line x1={sx(operating.cost)} y1={M.top} x2={sx(operating.cost)} y2={M.top + plotH} stroke="#C0452A" strokeWidth="1" strokeDasharray="3 2" />
            <line x1={M.left} y1={sy(operating.accuracy)} x2={W - M.right} y2={sy(operating.accuracy)} stroke="#C0452A" strokeWidth="1" strokeDasharray="3 2" />
            <circle cx={sx(operating.cost)} cy={sy(operating.accuracy)} r="5" fill="none" stroke="#C0452A" strokeWidth="1.6" />
            <text x={W - M.right + 8} y={sy(operating.accuracy) - 4} fontSize="12" fill="#C0452A" fontWeight="500">
              B3
            </text>
            <text x={W - M.right + 8} y={sy(operating.accuracy) + 10} fontSize="11" fill="#5E6560">
              {usd(operating.cost, 5)}
            </text>
            <text x={W - M.right + 8} y={sy(operating.accuracy) + 23} fontSize="11" fill="#5E6560">
              {pct(operating.accuracy, 1)}
            </text>
          </g>
        )}

        <line x1={M.left} y1={M.top + plotH} x2={W - M.right} y2={M.top + plotH} stroke="#15181A" strokeWidth="1" />
        <line x1={M.left} y1={M.top} x2={M.left} y2={M.top + plotH} stroke="#15181A" strokeWidth="1" />
        <text x={M.left + plotW / 2} y={H - 6} textAnchor="middle" fontSize="11" fill="#5E6560">
          Cost per task
        </text>
        <text x={14} y={M.top + plotH / 2} textAnchor="middle" fontSize="11" fill="#5E6560" transform={`rotate(-90 14 ${M.top + plotH / 2})`}>
          Accuracy
        </text>

        {hover !== null && data[hover] && (
          <g pointerEvents="none">
            <rect x={sx(data[hover].cost) + 6} y={sy(data[hover].accuracy) - 34} width="128" height="30" fill="#FFFFFF" stroke="#15181A33" />
            <text x={sx(data[hover].cost) + 12} y={sy(data[hover].accuracy) - 21} fontSize="11" fill="#15181A">
              {usd(data[hover].cost, 5)} · {pct(data[hover].accuracy, 1)}
            </text>
            <text x={sx(data[hover].cost) + 12} y={sy(data[hover].accuracy) - 9} fontSize="10" fill="#5E6560">
              thresholds {data[hover].thresholds.slice(0, -1).map((t) => t.toFixed(2)).join(", ")}
            </text>
          </g>
        )}
      </svg>
      <figcaption className="text-small text-graphite mt-3 max-w-prose">
        {operatingNote} Each point is one threshold setting, evaluated on the test split from
        recorded responses rather than by making more calls.
      </figcaption>
    </figure>
  );
}
