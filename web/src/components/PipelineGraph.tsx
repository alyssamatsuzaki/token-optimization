/**
 * The pipeline graph, and the one orchestrated motion in the product: the morph from baseline
 * to candidate (docs/DESIGN.md section 4).
 *
 * Node weight scales with share of run cost, so the expensive step is the big one without
 * anybody having to read a number first. Tiers ride a lightness ramp rather than a hue each,
 * and scarcity is a hatch, so the two encodings never collide.
 */
import { useMemo, type ReactNode } from "react";
import {
  Background,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { count, ms, pct, tierTint, tokens, usdShort } from "../lib/format";
import type { Provenance } from "../lib/types";
import { Figure } from "./Primitives";

export interface StepNode {
  id: string;
  label: string;
  model: string;
  calls: number;
  costUsd: number;
  costShare: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  p50LatencyMs: number | null;
  scarce: boolean;
  tierIndex: number;
  tierCount: number;
}

interface StepData extends Record<string, unknown> {
  step: StepNode;
  mark: Provenance;
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <dt className="text-graphite shrink-0">{label}</dt>
      <dd className="text-right tabular-nums min-w-0">{value}</dd>
    </div>
  );
}

function StepCard({ data }: NodeProps<Node<StepData>>) {
  const { step, mark } = data;
  const weight = 0.35 + 0.65 * Math.min(1, step.costShare);
  return (
    <div
      className="bg-chalk border text-ink"
      style={{
        borderColor: tierTint(step.tierIndex, step.tierCount),
        borderWidth: `${(1 + weight * 2).toFixed(1)}px`,
        width: 210,
      }}
      data-testid={`graph-node-${step.id}`}
    >
      <Handle type="target" position={Position.Left} className="!bg-graphite !w-1.5 !h-1.5" />
      <div
        className="px-2.5 py-1.5 flex items-baseline justify-between gap-2"
        style={{ background: tierTint(step.tierIndex, step.tierCount), color: "#FFFFFF" }}
      >
        <span className="text-small font-medium truncate">{step.label}</span>
        {step.scarce && (
          <span
            className="hatch-scarce w-3 h-3 shrink-0 border border-vermilion"
            title="Scarce model: capacity is the constraint, not the dollars"
            aria-label="scarce model"
          />
        )}
      </div>
      <dl className="px-2.5 py-2 text-micro leading-relaxed">
        <Row label="model" value={<span className="truncate">{step.model}</span>} />
        <Row label="calls" value={count(step.calls)} />
        {/* A node with no spend of its own prints neither a cost nor a share: "$0.00000" and
            "0%" are decoration, not information. */}
        {step.costUsd > 0 && (
          <>
            <Row label="cost" value={<Figure value={usdShort(step.costUsd)} kind={mark} />} />
            <Row label="share of run" value={pct(step.costShare, 0)} />
          </>
        )}
        <Row label="input" value={tokens(step.inputTokens)} />
        {step.outputTokens > 0 && <Row label="output" value={tokens(step.outputTokens)} />}
        <Row
          label="cache read"
          value={step.cacheReadTokens ? tokens(step.cacheReadTokens) : "none"}
        />
        {step.p50LatencyMs !== null && <Row label="p50 latency" value={ms(step.p50LatencyMs)} />}
      </dl>
      <Handle type="source" position={Position.Right} className="!bg-graphite !w-1.5 !h-1.5" />
    </div>
  );
}

const nodeTypes = { step: StepCard };

export function PipelineGraph({
  steps,
  edges,
  mark,
  testId,
}: {
  steps: StepNode[];
  edges: { from: string; to: string; label?: string }[];
  mark: Provenance;
  testId: string;
}) {
  const nodes = useMemo<Node<StepData>[]>(
    () =>
      steps.map((step, index) => ({
        id: step.id,
        type: "step",
        position: { x: index * 268, y: step.tierIndex * 24 },
        data: { step, mark },
        draggable: false,
        selectable: false,
      })),
    [steps, mark],
  );

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge) => ({
        id: `${edge.from}->${edge.to}`,
        source: edge.from,
        target: edge.to,
        label: edge.label,
        animated: false,
        style: { stroke: "#5E6560", strokeWidth: 1 },
        labelStyle: { fill: "#15181A", fontSize: 11 },
        labelBgStyle: { fill: "#F2F3EF" },
      })),
    [edges],
  );

  return (
    <div
      className="h-[320px] border border-rule"
      data-testid={testId}
      aria-label="Pipeline graph"
    >
      <ReactFlow
        nodes={nodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        zoomOnScroll={false}
      >
        <Background color="#15181A14" gap={22} size={1} />
      </ReactFlow>
    </div>
  );
}
